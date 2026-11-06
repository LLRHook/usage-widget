//! Batch Claude usage reduction. Filesystem access, Python's permissive JSON
//! coercions, pricing tables, and local-time/DST interpretation stay in the host.
//! This library has no filesystem, network, clock, or account access.

use num_bigint::BigInt;
use serde_json::{json, Map, Number, Value};
use std::collections::{HashMap, HashSet};
use std::ffi::{c_char, CString};
use std::panic::catch_unwind;
use std::str::FromStr;

const TOKEN_KEYS: [&str; 4] = ["input", "output", "cache_w", "cache_r"];

type Result<T> = std::result::Result<T, String>;

fn field<'a>(v: &'a Value, key: &str) -> Result<&'a Value> {
    v.get(key).ok_or_else(|| format!("missing {key}"))
}

fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    field(v, key)?
        .as_str()
        .ok_or_else(|| format!("invalid {key}"))
}

fn array<'a>(v: &'a Value, key: &str) -> Result<&'a Vec<Value>> {
    field(v, key)?
        .as_array()
        .ok_or_else(|| format!("invalid {key}"))
}

fn boolean(v: &Value, key: &str) -> Result<bool> {
    field(v, key)?
        .as_bool()
        .ok_or_else(|| format!("invalid {key}"))
}

fn optional_text<'a>(v: &'a Value, key: &str) -> Result<Option<&'a str>> {
    match field(v, key)? {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s)),
        _ => Err(format!("invalid {key}")),
    }
}

fn integer(v: &Value) -> Result<BigInt> {
    let s = v.as_str().ok_or("invalid token count")?;
    BigInt::from_str(s).map_err(|_| "invalid token count".into())
}

fn number(i: &BigInt) -> Value {
    // The arbitrary_precision feature keeps Python-sized integers as JSON
    // integers, rather than narrowing them to i64 or silently rounding to f64.
    Value::Number(Number::from_str(&i.to_string()).expect("BigInt is a JSON integer"))
}

fn float(f: f64) -> Value {
    // Python's existing cost arithmetic permits non-finite intermediate sums.
    // Encode these explicitly on the private wire; the host restores floats.
    Number::from_f64(f).map(Value::Number).unwrap_or_else(|| {
        Value::String(
            if f.is_nan() {
                "NaN"
            } else if f.is_sign_positive() {
                "Infinity"
            } else {
                "-Infinity"
            }
            .into(),
        )
    })
}

#[derive(Default)]
struct Bucket {
    tokens: [BigInt; 4],
    cost: f64,
    messages: u64,
    by_model: Ordered<BigInt>,
}

impl Bucket {
    fn add(&mut self, tokens: &[BigInt; 4], cost: f64) {
        for (sum, n) in self.tokens.iter_mut().zip(tokens) {
            *sum += n;
        }
        self.cost += cost;
        self.messages += 1;
    }

    fn value(&self, daily: bool) -> Value {
        let mut out = Map::new();
        for (key, n) in TOKEN_KEYS.iter().zip(&self.tokens) {
            out.insert((*key).into(), number(n));
        }
        out.insert("cost".into(), float(self.cost));
        out.insert("messages".into(), self.messages.into());
        if daily {
            out.insert(
                "by_model".into(),
                json!(self
                    .by_model
                    .entries
                    .iter()
                    .map(|(k, n)| (k, number(n)))
                    .collect::<Vec<_>>()),
            );
        }
        Value::Object(out)
    }
}

// A Vec retains encounter order for stable top-N ties, just like Python dicts.
// The lookup map avoids quadratic bucket search on long histories.
#[derive(Default)]
struct Ordered<T> {
    indices: HashMap<String, usize>,
    entries: Vec<(String, T)>,
}

impl<T: Default> Ordered<T> {
    fn get(&mut self, name: &str) -> &mut T {
        let index = *self.indices.entry(name.into()).or_insert_with(|| {
            self.entries.push((name.into(), T::default()));
            self.entries.len() - 1
        });
        &mut self.entries[index].1
    }
}

#[derive(Default)]
struct Project {
    tokens: BigInt,
    cost: f64,
    messages: u64,
}

fn cost(tokens: &[BigInt; 4], prices: &[Value]) -> Result<f64> {
    if prices.len() != 4 {
        return Err("invalid price count".into());
    }
    let mut values = [0.0; 4];
    for ((v, n), p) in values.iter_mut().zip(tokens).zip(prices) {
        // Decimal parsing rounds to the nearest binary64, like float(int).
        let n: f64 = n.to_string().parse().map_err(|_| "integer overflow")?;
        if !n.is_finite() {
            return Err("integer overflow".into());
        }
        let p = p
            .as_f64()
            .filter(|p| p.is_finite())
            .ok_or("invalid price")?;
        *v = n * p;
    }
    // Preserve Python's evaluation order: input, cache write, cache read, output.
    Ok(((values[0] + values[2]) + values[3] + values[1]) / 1_000_000.0)
}

fn reduce(request: &Value) -> Result<Value> {
    if field(request, "version")?.as_u64() != Some(1) {
        return Err("unsupported protocol version".into());
    }
    let summary = boolean(request, "summary")?;
    let window_count = field(request, "window_count")?
        .as_u64()
        .filter(|n| *n <= 1024)
        .ok_or("invalid window count")? as usize;
    let records = array(request, "records")?;
    let prices = field(request, "prices")?
        .as_object()
        .ok_or("invalid prices")?;
    let mut days: Ordered<Bucket> = Ordered::default();
    let mut models: Ordered<Bucket> = Ordered::default();
    let mut projects: Ordered<Project> = Ordered::default();
    let mut tools: Ordered<u64> = Ordered::default();
    let mut totals = Bucket::default();
    let mut sessions = HashSet::new();
    let mut seen = HashSet::new();
    let mut window_seen: Vec<HashSet<&str>> = (0..window_count).map(|_| HashSet::new()).collect();
    let mut windows = vec![BigInt::default(); window_count];
    let mut heatmap: Vec<Vec<BigInt>> = vec![vec![BigInt::default(); 24]; 7];
    let mut burn_tokens = BigInt::default();
    let mut burn_cost = 0.0;

    for record in records {
        if summary {
            for tool in array(record, "tools")? {
                let name = tool.as_str().ok_or("invalid tool name")?;
                *tools.get(name) += 1;
            }
        }
        let usage = field(record, "usage")?;
        if usage.is_null() {
            continue;
        }
        let usage = usage
            .as_array()
            .filter(|a| a.len() == 4)
            .ok_or("invalid usage")?;
        let tokens: [BigInt; 4] = [
            integer(&usage[0])?,
            integer(&usage[1])?,
            integer(&usage[2])?,
            integer(&usage[3])?,
        ];
        let tok: BigInt = tokens.iter().sum();
        let id = optional_text(record, "id")?.filter(|s| !s.is_empty());
        let duplicate = id.is_some_and(|id| !seen.insert(id));
        let time = field(record, "time")?;

        // Summary dedup happens before validating time in the legacy collector.
        // Windows validate time first and keep their own dedup sets, so an old
        // or invalid first copy must not suppress an eligible later copy.
        if let Some(error) = time.get("error").and_then(Value::as_str) {
            if (summary && !duplicate) || window_count > 0 {
                return Err(format!("time error:{error}"));
            }
            continue;
        }
        if !time.is_null() {
            let eligible = array(time, "windows")?;
            if eligible.len() != window_count {
                return Err("invalid window flags".into());
            }
            for ((n, ids), flag) in windows.iter_mut().zip(&mut window_seen).zip(eligible) {
                if flag.as_bool().ok_or("invalid window flag")?
                    && id.is_none_or(|id| ids.insert(id))
                {
                    *n += &tok;
                }
            }
        }
        if !summary || duplicate || time.is_null() {
            continue;
        }
        let fam = text(record, "family")?;
        let price = prices
            .get(fam)
            .and_then(Value::as_array)
            .ok_or("invalid model price")?;
        let c = cost(&tokens, price)?;
        let day = text(time, "day")?;
        let b = days.get(day);
        b.add(&tokens, c);
        *b.by_model.get(fam) += &tok;
        models.get(fam).add(&tokens, c);
        let p = projects.get(text(record, "project")?);
        p.tokens += &tok;
        p.cost += c;
        p.messages += 1;
        let offset = field(time, "offset")?
            .as_i64()
            .ok_or("invalid day offset")?;
        let hour = field(time, "hour")?
            .as_u64()
            .filter(|h| *h < 24)
            .ok_or("invalid hour")? as usize;
        if (0..7).contains(&offset) {
            heatmap[offset as usize][hour] += &tok;
        }
        if boolean(time, "burn")? {
            burn_tokens += &tok;
            burn_cost += c;
        }
        totals.add(&tokens, c);
        if let Some(session) = optional_text(record, "session")?.filter(|s| !s.is_empty()) {
            sessions.insert(session);
        }
    }
    projects.entries.sort_by(|a, b| b.1.tokens.cmp(&a.1.tokens));
    tools.entries.sort_by(|a, b| b.1.cmp(&a.1));
    let mut totals = totals.value(false);
    totals["sessions"] = sessions.len().into();
    Ok(json!({
        "summary": {
            "by_day": days.entries.iter().map(|(k,b)| (k.clone(),b.value(true))).collect::<Vec<_>>(),
            "by_model": models.entries.iter().map(|(k,b)| (k.clone(),b.value(false))).collect::<Vec<_>>(),
            "projects": projects.entries.iter().take(6).map(|(k,p)| json!({"name":k,"tokens":number(&p.tokens),"cost":float(p.cost),"messages":p.messages})).collect::<Vec<_>>(),
            "tools": tools.entries.iter().take(8).map(|(k,n)| json!({"name":k,"count":n})).collect::<Vec<_>>(),
            "heatmap": heatmap.iter().map(|row| row.iter().map(number).collect::<Vec<_>>()).collect::<Vec<_>>(),
            "burn": {"tokens_last_60min":number(&burn_tokens),"cost_last_60min":float(burn_cost)},
            "totals":totals,"found":true
        },
        "windows":windows.iter().map(number).collect::<Vec<_>>()
    }))
}

fn response(bytes: &[u8]) -> Value {
    match serde_json::from_slice(bytes)
        .map_err(|e| e.to_string())
        .and_then(|v| reduce(&v))
    {
        Ok(value) => json!({"version":1,"ok":value}),
        Err(error) => json!({"version":1,"error":error}),
    }
}

#[no_mangle]
pub extern "C" fn ai_usage_core_version() -> u32 {
    1
}

/// Return an owned NUL-terminated UTF-8 JSON response, including on protocol errors.
///
/// # Safety
/// `data` must point to `len` readable bytes for this call, or be null with len 0.
/// Free the result exactly once with `ai_usage_core_free`; never mutate its bytes.
#[no_mangle]
pub unsafe extern "C" fn ai_usage_core_reduce(data: *const u8, len: usize) -> *mut c_char {
    let result = catch_unwind(|| {
        if data.is_null() {
            return json!({"version":1,"error":"null request"});
        }
        // SAFETY: The caller provides the readable buffer described above.
        response(unsafe { std::slice::from_raw_parts(data, len) })
    })
    .unwrap_or_else(|_| json!({"version":1,"error":"native reducer panicked"}));
    CString::new(result.to_string())
        .expect("JSON escapes NUL bytes")
        .into_raw()
}

/// Release a response returned by `ai_usage_core_reduce`. A null pointer is a no-op.
///
/// # Safety
/// A non-null pointer must be an unmodified, not-yet-freed response from this library.
#[no_mangle]
pub unsafe extern "C" fn ai_usage_core_free(ptr: *mut c_char) {
    if !ptr.is_null() {
        // SAFETY: Ownership is returned once by the caller, using this allocator.
        drop(unsafe { CString::from_raw(ptr) });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CStr;

    #[test]
    fn ffi_errors_are_owned_and_releasable() {
        for input in [b"{broken".as_slice(), b"{}", b"[]", b"null", b"\xff"] {
            // SAFETY: The slice stays alive and the returned response is freed once.
            unsafe {
                let ptr = ai_usage_core_reduce(input.as_ptr(), input.len());
                let value: Value = serde_json::from_slice(CStr::from_ptr(ptr).to_bytes()).unwrap();
                assert!(value["error"].is_string());
                ai_usage_core_free(ptr);
            }
        }
    }

    #[test]
    fn ffi_null_input_is_recoverable() {
        // SAFETY: Null input is explicitly supported; the response is freed once.
        unsafe {
            let ptr = ai_usage_core_reduce(std::ptr::null(), 0);
            assert!(CStr::from_ptr(ptr)
                .to_str()
                .unwrap()
                .contains("null request"));
            ai_usage_core_free(ptr);
            ai_usage_core_free(std::ptr::null_mut());
        }
    }

    #[test]
    fn integers_remain_signed_and_unbounded_on_wire() {
        let n = BigInt::from_str("-99999999999999999999999999999999999999999").unwrap();
        assert_eq!(number(&n).to_string(), n.to_string());
    }
}
