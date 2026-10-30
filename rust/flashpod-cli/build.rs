//! Embed the bundled ipod-patches snapshot (flashpod/firmware/patches,
//! written by scripts/sync_patches_snapshot.py): every file its index.json
//! lists, so the offline fallback ships inside the binary.

use std::fmt::Write;
use std::path::Path;

fn main() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../flashpod/firmware/patches");
    let index_path = root.join("index.json");
    println!("cargo:rerun-if-changed={}", index_path.display());
    let index: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&index_path).expect("snapshot index.json")).expect("snapshot index");
    let mut out = String::from("/// The bundled snapshot: (path, bytes), index.json included.\npub static BUNDLED: &[(&str, &[u8])] = &[\n");
    let mut paths = vec!["index.json".to_string()];
    for f in index["files"].as_array().expect("files") {
        paths.push(f["path"].as_str().expect("path").to_string());
    }
    for p in &paths {
        let full = root.join(p);
        println!("cargo:rerun-if-changed={}", full.display());
        writeln!(out, "    ({:?}, include_bytes!({:?})),", p, full.display().to_string()).unwrap();
    }
    out.push_str("];\n");
    let dst = Path::new(&std::env::var("OUT_DIR").unwrap()).join("bundled_patches.rs");
    std::fs::write(dst, out).unwrap();
}
