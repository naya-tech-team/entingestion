#!/usr/bin/env node
// License gate for the web UI: every installed npm package must be open source under an
// approved license. The npm counterpart of tools/check_licenses.py, with the same policy:
// - Allowed: permissive (MIT, BSD, Apache-2.0, ISC, PSF, Zlib, Unlicense, CC0, HPND, BlueOak)
//   and weak copyleft used unmodified as a library (LGPL, MPL).
// - Denied: strong or network copyleft (GPL, AGPL), source-available (SSPL, BUSL, Commons
//   Clause), commercial/proprietary, UNLICENSED and "SEE LICENSE IN ..." (custom terms).
// - SPDX expressions: "A OR B" passes if any alternative is allowed (we can take it under that
//   one); "A AND B" needs all; "A WITH exception" is judged by A. A legacy `licenses` array is
//   read as alternatives.
// - Unknown or missing licenses fail until reviewed and added to REVIEWED below.
//
// No dependencies. Walks node_modules (nested and scoped) and reads each package.json.
// Usage (from services/ui/web, after npm ci):  node ../../../tools/check_npm_licenses.mjs [node_modules dir]

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ALLOWED = [
  "MIT",
  "BSD",
  "APACHE",
  "ISC",
  "PSF",
  "PYTHON SOFTWARE FOUNDATION",
  "ZLIB",
  "UNLICENSE",
  "CC0",
  "HPND",
  "HISTORICAL PERMISSION NOTICE",
  "CNRI-PYTHON",
  "BLUEOAK", // Blue Oak Model License: permissive, OSI-approved, common on npm
  "MPL",
  "MOZILLA PUBLIC LICENSE",
  "LGPL",
  "LESSER GENERAL PUBLIC LICENSE",
];
const DENIED = [
  "AGPL",
  "AFFERO",
  "SSPL",
  "BUSL",
  "BUSINESS SOURCE",
  "COMMONS CLAUSE",
  "COMMERCIAL",
  "PROPRIETARY",
  "UNLICENSED", // npm's "not licensed for use by others" (not the Unlicense)
  "SEE LICENSE",
];

// Our own packages (not distributed as dependencies).
const OWN = new Set(["entingestion-ui-web"]);

// Packages whose metadata doesn't state a usable license, reviewed by hand:
// "name" (or "name@version") -> "license: where it was checked and why it's fine".
const REVIEWED = {};

export function leafAllowed(text) {
  const upper = text.toUpperCase();
  if (DENIED.some((d) => upper.includes(d))) return false;
  if (/(?<!L)GPL|(?<!LESSER )GENERAL PUBLIC LICENSE/.test(upper)) return false; // GPL, not LGPL
  return ALLOWED.some((a) => upper.includes(a));
}

/** Tokens of an SPDX expression: "(", ")", "AND", "OR", "WITH" and license ids. */
function tokenize(expr) {
  return expr
    .replace(/[()]/g, " $& ")
    .split(/\s+/)
    .filter(Boolean);
}

/** Evaluates an SPDX expression (AND binds tighter than OR). Throws on malformed input. */
export function expressionAllowed(expr) {
  const tokens = tokenize(expr);
  let i = 0;
  const peek = () => tokens[i]?.toUpperCase();
  function primary() {
    const t = tokens[i++];
    if (t === undefined) throw new Error("unexpected end");
    if (t === "(") {
      const v = orExpr();
      if (tokens[i++] !== ")") throw new Error("missing )");
      return v;
    }
    if (t === ")" || ["AND", "OR", "WITH"].includes(t.toUpperCase())) throw new Error(`unexpected ${t}`);
    if (peek() === "WITH") {
      i++; // the exception only adds permissions; judge the license itself
      if (tokens[i++] === undefined) throw new Error("missing exception");
    }
    return leafAllowed(t);
  }
  function andExpr() {
    let v = primary();
    while (peek() === "AND") {
      i++;
      v = primary() && v;
    }
    return v;
  }
  function orExpr() {
    let v = andExpr();
    while (peek() === "OR") {
      i++;
      v = andExpr() || v;
    }
    return v;
  }
  const result = orExpr();
  if (i !== tokens.length) throw new Error(`unexpected ${tokens[i]}`);
  return result;
}

/** The license text a package.json declares, or null. */
export function declaredLicense(pkg) {
  const lic = pkg.license;
  if (typeof lic === "string" && lic.trim()) return lic.trim();
  if (lic && typeof lic === "object" && typeof lic.type === "string") return lic.type.trim();
  if (Array.isArray(pkg.licenses) && pkg.licenses.length > 0) {
    const alts = pkg.licenses
      .map((l) => (typeof l === "string" ? l : l && typeof l.type === "string" ? l.type : ""))
      .map((s) => s.trim())
      .filter(Boolean);
    if (alts.length === 1) return alts[0];
    if (alts.length > 1) return `(${alts.join(" OR ")})`;
  }
  return null;
}

export function licenseAllowed(license) {
  try {
    return expressionAllowed(license);
  } catch {
    return leafAllowed(license); // not SPDX: judge the free text as Python does
  }
}

/** Every installed package: {name, version, license, dir}. */
export function installedPackages(nodeModules) {
  const found = [];
  const visit = (dir) => {
    if (!existsSync(dir)) return;
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith(".")) continue; // .bin, .package-lock.json, .vite ...
      if (!entry.isDirectory() && !entry.isSymbolicLink()) continue;
      const full = join(dir, entry.name);
      if (entry.name.startsWith("@")) {
        visit(full); // a scope: its children are packages
        continue;
      }
      const manifest = join(full, "package.json");
      if (!existsSync(manifest)) continue;
      let pkg;
      try {
        pkg = JSON.parse(readFileSync(manifest, "utf8"));
      } catch (err) {
        found.push({ name: entry.name, version: "?", license: null, dir: full, error: String(err) });
        continue;
      }
      found.push({ name: pkg.name ?? entry.name, version: pkg.version ?? "?", license: declaredLicense(pkg), dir: full });
      visit(join(full, "node_modules"));
    }
  };
  visit(nodeModules);
  return found;
}

function table(rows) {
  const widths = rows[0].map((_, c) => Math.max(...rows.map((r) => String(r[c]).length)));
  return rows.map((r) => r.map((v, c) => String(v).padEnd(widths[c])).join("  ").trimEnd()).join("\n");
}

function main() {
  const here = dirname(fileURLToPath(import.meta.url));
  const nodeModules = resolve(process.argv[2] ?? join(here, "..", "services", "ui", "web", "node_modules"));
  if (!existsSync(nodeModules)) {
    console.error(`npm license check: ${nodeModules} does not exist (run npm ci first)`);
    return 2;
  }
  const seen = new Set();
  const failures = [];
  const byLicense = new Map();
  let checked = 0;
  for (const p of installedPackages(nodeModules)) {
    const key = `${p.name}@${p.version}`;
    if (OWN.has(p.name) || seen.has(key)) continue;
    seen.add(key);
    checked++;
    const reviewed = REVIEWED[key] ?? REVIEWED[p.name];
    if (reviewed) {
      byLicense.set("reviewed", (byLicense.get("reviewed") ?? 0) + 1);
      continue;
    }
    const where = relative(nodeModules, p.dir);
    if (!p.license) {
      failures.push([key, p.error ? "unreadable package.json" : "no license metadata (review it and add to REVIEWED)", where]);
    } else if (!licenseAllowed(p.license)) {
      failures.push([key, p.license, where]);
    } else {
      byLicense.set(p.license, (byLicense.get(p.license) ?? 0) + 1);
    }
  }
  if (failures.length > 0) {
    console.log(`npm license check FAILED (${failures.length} of ${checked} packages):\n`);
    console.log(table([["package", "license", "path"], ...failures]));
    return 1;
  }
  const summary = [...byLicense.entries()].sort((a, b) => b[1] - a[1]).map(([l, n]) => `${l}: ${n}`);
  console.log(`npm license check passed: ${checked} packages, all open source under approved licenses (${summary.join(", ")}).`);
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main());
}
