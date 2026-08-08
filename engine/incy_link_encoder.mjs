import { encryptLink } from "@incy/link-encoder";

const MAX_STDIN_BYTES = 64 * 1024;
const chunks = [];
let totalBytes = 0;

for await (const chunk of process.stdin) {
  totalBytes += chunk.length;
  if (totalBytes > MAX_STDIN_BYTES) {
    console.error("payload too large");
    process.exit(1);
  }
  chunks.push(chunk);
}

let payload;
try {
  payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
} catch {
  console.error("invalid json payload");
  process.exit(1);
}

if (!payload || typeof payload.url !== "string" || payload.url.length === 0) {
  console.error("url is required");
  process.exit(1);
}

const options = {};
if (typeof payload.name === "string" && payload.name.length > 0) {
  options.name = payload.name;
}

try {
  process.stdout.write(encryptLink(payload.url, options));
} catch {
  console.error("incy link encryption failed");
  process.exit(1);
}
