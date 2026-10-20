// SPDX-License-Identifier: AGPL-3.0-or-later
fn main() {
    let compiler = std::env::var("RUSTC").unwrap();
    let output = std::process::Command::new(compiler)
        .arg("--version")
        .output()
        .unwrap();
    assert!(output.status.success());
    println!(
        "cargo:rustc-env=BENCH_RUSTC={}",
        String::from_utf8(output.stdout).unwrap().trim()
    );
    println!("cargo:rerun-if-changed=build.rs");
}
