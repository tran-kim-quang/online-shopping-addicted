import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const messagesDir = path.resolve(__dirname, "..", "messages");
const viPath = path.join(messagesDir, "vi.json");
const enPath = path.join(messagesDir, "en.json");

function flatten(obj, prefix = "") {
  const result = new Map();
  for (const [key, value] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      for (const [childKey, childVal] of flatten(value, fullKey)) {
        result.set(childKey, childVal);
      }
    } else if (typeof value === "string") {
      result.set(fullKey, value);
    } else {
      throw new Error(`Invalid i18n value type at key "${fullKey}": expected string or object`);
    }
  }
  return result;
}

function extractPlaceholders(text) {
  const matches = text.matchAll(/\{([a-zA-Z0-9_]+)(?:,[^}]*)?\}/g);
  return [...new Set([...matches].map((m) => m[1]))].sort();
}

const viJson = JSON.parse(fs.readFileSync(viPath, "utf8"));
const enJson = JSON.parse(fs.readFileSync(enPath, "utf8"));

const viKeys = flatten(viJson);
const enKeys = flatten(enJson);

const errors = [];

for (const key of viKeys.keys()) {
  if (!enKeys.has(key)) {
    errors.push(`Missing key in en.json: ${key}`);
  }
}

for (const key of enKeys.keys()) {
  if (!viKeys.has(key)) {
    errors.push(`Extra key in en.json (missing in vi.json): ${key}`);
  }
}

for (const [key, viVal] of viKeys.entries()) {
  if (viVal.trim().length === 0) {
    errors.push(`Empty value in vi.json: ${key}`);
  }
  const enVal = enKeys.get(key);
  if (typeof enVal === "string") {
    if (enVal.trim().length === 0) {
      errors.push(`Empty value in en.json: ${key}`);
    }
    const viVars = extractPlaceholders(viVal).join(",");
    const enVars = extractPlaceholders(enVal).join(",");
    if (viVars !== enVars) {
      errors.push(
        `Placeholder mismatch at "${key}": vi=[${viVars}] vs en=[${enVars}]`,
      );
    }
  }
}

if (errors.length > 0) {
  console.error("i18n check failed:\n" + errors.map((e) => `  - ${e}`).join("\n"));
  process.exit(1);
}

console.log(`i18n check passed: ${viKeys.size} keys verified across vi.json and en.json.`);
