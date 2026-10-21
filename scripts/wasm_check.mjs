// Run one flashpod_core::api command in the WebAssembly build, under Node, as
// `fpcore run` does natively, for scripts/rust_parity.py to compare:
//
//   node scripts/wasm_check.mjs WASM CARD JSON [--blob FILE] [--events] [--cancel-after N]
//
// The host is what a browser Worker gives the module: synchronous reads and
// writes on the card file (here Node's fs, there an OPFS access handle), the
// music files by name, progress and cancel. Prints the value (with the
// recorded events when --events) and exits 0, or prints the error, exits 1.
import fs from "node:fs";

const [wasmPath, cardPath, cmdText, ...rest] = process.argv.slice(2);
const opt = (name) => {
  const i = rest.indexOf(name);
  return i < 0 ? null : rest[i + 1];
};
const blob = opt("--blob") ? fs.readFileSync(opt("--blob")) : new Uint8Array(0);
const cancelAfter = opt("--cancel-after") === null ? null : Number(opt("--cancel-after"));
const events = [];
let files = 0;

const op = JSON.parse(cmdText).op;
const readOnly = ["inspect", "tracks", "plan_add", "min_card_bytes", "catalog", "check_firmware"].includes(op);
const card = fs.openSync(cardPath, readOnly ? "r" : "r+");
const utf8 = new TextDecoder();
let mem;
const bytes = (ptr, len) => new Uint8Array(mem.buffer, ptr, len);
const text = (ptr, len) => utf8.decode(bytes(ptr, len));
const sources = new Map();
const source = (name) => {
  if (!sources.has(name)) {
    let fd = -1;
    try {
      if (fs.statSync(name).isFile()) fd = fs.openSync(name, "r");
    } catch (e) {}
    sources.set(name, fd);
  }
  return sources.get(name);
};

const host = {
  dev_len: () => fs.fstatSync(card).size,
  dev_read: (off, ptr, len) => {
    try {
      return fs.readSync(card, bytes(ptr, len), 0, len, off);
    } catch (e) {
      return -1;
    }
  },
  dev_write: (off, ptr, len) => {
    try {
      let done = 0;
      while (done < len) done += fs.writeSync(card, bytes(ptr + done, len - done), 0, len - done, off + done);
      return 0;
    } catch (e) {
      return -1;
    }
  },
  dev_flush: () => 0,
  dev_set_len: (len) => {
    try {
      fs.ftruncateSync(card, len);
      return 0;
    } catch (e) {
      return -1;
    }
  },
  src_size: (p, n) => {
    const fd = source(text(p, n));
    return fd < 0 ? -1 : fs.fstatSync(fd).size;
  },
  src_read: (p, n, off, ptr, len) => {
    const fd = source(text(p, n));
    if (fd < 0) return -1;
    try {
      return fs.readSync(fd, bytes(ptr, len), 0, len, off);
    } catch (e) {
      return -1;
    }
  },
  progress: (sp, sn, done, total, ip, iname) => {
    const stage = text(sp, sn);
    if (stage === "tags" || stage === "copy") files += 1;
    events.push([stage, done, total, text(ip, iname)]);
  },
  cancelled: () => (cancelAfter !== null && files >= cancelAfter ? 1 : 0),
};

const { instance } = await WebAssembly.instantiate(fs.readFileSync(wasmPath), { host });
const ex = instance.exports;
mem = ex.memory;
const put = (data) => {
  const p = ex.alloc(data.length);
  bytes(p, data.length).set(data);
  return p;
};
const cmd = new TextEncoder().encode(cmdText);
const cp = put(cmd);
const bp = blob.length ? put(blob) : 0;
ex.run(cp, cmd.length, bp, blob.length);
const answer = JSON.parse(text(ex.result_ptr(), ex.result_len()));
fs.closeSync(card);
if (answer.ok) {
  const v = answer.value;
  if (rest.includes("--events")) v.events = events;
  console.log(JSON.stringify(v));
} else {
  console.log(JSON.stringify(answer.error));
  process.exit(1);
}
