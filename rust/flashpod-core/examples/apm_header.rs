//! Print-free: write build_apm_layout(TOTAL)'s blocks 0..62 to PATH.
//!   apm_header PATH TOTAL_SECTORS
fn main() {
    let a: Vec<String> = std::env::args().collect();
    let (hdr, _, _) = flashpod_core::layout::build_apm_layout(a[2].parse().unwrap(), false, None).unwrap();
    std::fs::write(&a[1], hdr).unwrap();
}
