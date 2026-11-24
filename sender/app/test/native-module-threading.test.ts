import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { join, relative, sep } from "node:path";
import { test } from "node:test";

// Sender native modules pass owned JVM values across Expo's async queues.
// Keep live JSI handles out of their helpers as well as AsyncFunction signatures.
// This is a source guard, not a Kotlin parser or a Hermes threading test.
function jsHandleReferences(source: string): string[] {
  const code = source.replace(
    /"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    " ",
  );
  return [...code.matchAll(
    /\bexpo\s*\.\s*modules\s*\.\s*kotlin\s*\.\s*(?:typedarray\s*\.\s*(?:\w+|\*)|jni\s*\.\s*(?:JavaScript\w*|\*))/g,
  )].map((match) => match[0]);
}

test("Sender Android modules use owned values instead of live JSI handles", () => {
  const root = fileURLToPath(new URL("../modules/", import.meta.url));
  const files = readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".kt"))
    .map((entry) => join(entry.parentPath, entry.name))
    .filter((path) => relative(root, path).includes(`${sep}android${sep}src${sep}main${sep}`));
  assert.ok(files.length > 0, "Expected production Android Kotlin sources");
  const violations = files.flatMap((path) => jsHandleReferences(readFileSync(path, "utf8"))
    .map((reference) => `${relative(root, path)}: ${reference}`));
  assert.deepEqual(violations, [],
    "Live JSI handles must not cross async queues; use Expo-converted JVM values such as ByteArray");
});

test("the guard detects aliased imports, wildcard imports, and qualified JSI types", () => {
  for (const reference of [
    "expo.modules.kotlin.typedarray.Uint8Array",
    "expo.modules.kotlin.typedarray.TypedArray",
    "expo.modules.kotlin.typedarray.*",
    "expo.modules.kotlin.jni.JavaScriptObject",
    "expo.modules.kotlin.jni.JavaScriptValue",
    "expo.modules.kotlin.jni.JavaScriptTypedArray",
    "expo.modules.kotlin.jni.JavaScriptArrayBuffer",
    "expo.modules.kotlin.jni.*",
  ]) {
    const alias = reference.endsWith("*") ? "" : " as Handle";
    assert.deepEqual(jsHandleReferences(`import ${reference}${alias}`), [reference]);
    if (!reference.endsWith("*")) {
      assert.deepEqual(jsHandleReferences(`AsyncFunction("synthetic") { value: ${reference} -> value }`), [reference]);
    }
  }
});

test("the guard permits copied byte arrays, comments, and string literals", () => {
  assert.deepEqual(jsHandleReferences(`
    // import expo.modules.kotlin.typedarray.Uint8Array
    /* expo.modules.kotlin.jni.JavaScriptObject */
    val explanation = "expo.modules.kotlin.jni.JavaScriptValue"
    val multiline = """expo.modules.kotlin.typedarray.TypedArray"""
    AsyncFunction("synthetic") { bytes: ByteArray, position: ByteArray? -> bytes }
  `), []);
});
