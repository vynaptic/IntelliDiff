// Install the locked web dependencies from the configured npm registry in the Node build stage.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, statSync, writeFileSync } from "node:fs";

const raw = process.env.MIRROR_NPM ?? "";
const registry = URL.canParse(raw) && new URL(raw);
if (!registry || !registry.hostname || registry.username || registry.password ||
    registry.search || registry.hash || /[\s\\]/.test(raw) ||
    !(registry.protocol === "https:" ||
      (registry.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(registry.hostname)))) {
  throw new Error("MIRROR_NPM must be a credential-free HTTPS registry URL");
}

const prefix = registry.href.replace(/\/+$/, "") + "/";
const lock = JSON.parse(readFileSync("package-lock.json", "utf8"));
for (const [path, pkg] of Object.entries(lock.packages)) {
  if (!path) continue; // the project
  const upstream = URL.canParse(pkg.resolved) && new URL(pkg.resolved);
  if (!upstream || upstream.origin !== "https://registry.npmjs.org" ||
      !pkg.integrity?.startsWith("sha512-")) {
    throw new Error(`expected a locked npm registry tarball and SHA-512 for ${path}`);
  }
  pkg.resolved = prefix + upstream.pathname.slice(1);
}
writeFileSync("package-lock.json", JSON.stringify(lock));
const ca = "/run/secrets/mirror_ca";
if (existsSync(ca) && statSync(ca).size) process.env.NODE_EXTRA_CA_CERTS = ca;
const result = spawnSync("npm", [
  "ci", "--registry", prefix, "--replace-registry-host=never", "--ignore-scripts",
  "--no-audit", "--no-fund", "--update-notifier=false",
], { stdio: "inherit" });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
