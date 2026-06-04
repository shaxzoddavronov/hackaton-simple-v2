/**
 * Phase 40 — guardrails on the i18n bundles.
 *
 * TypeScript already enforces structural identity (the `Messages`
 * type requires every key on every bundle), but a refactor that
 * drops a key from the type would silently weaken every locale at
 * once. These runtime checks lock the contract a second way:
 *
 *   1. Every bundle has IDENTICAL keys.
 *   2. No localised string is the literal placeholder "TODO".
 *   3. Every locale label is non-empty.
 *
 * Tests use a tiny harness so we don't pull in a heavy runner.
 */
import {
  LOCALES,
  LOCALE_LABEL,
  MESSAGES,
  type Locale,
} from "../messages";


function expect(condition: boolean, msg: string): void {
  if (!condition) {
    throw new Error(`assertion failed: ${msg}`);
  }
}


function keysOf(obj: Record<string, unknown>): string[] {
  return Object.keys(obj).sort();
}


function runBundleParity(): void {
  const reference = keysOf(MESSAGES.en);
  for (const loc of LOCALES) {
    const k = keysOf(MESSAGES[loc] as unknown as Record<string, unknown>);
    expect(
      JSON.stringify(k) === JSON.stringify(reference),
      `bundle "${loc}" key drift vs en: missing=` +
        reference.filter((x) => !k.includes(x)).join(",") +
        " extra=" +
        k.filter((x) => !reference.includes(x)).join(","),
    );
  }
}


function runNoPlaceholderTodo(): void {
  for (const loc of LOCALES) {
    const bundle = MESSAGES[loc] as unknown as Record<string, unknown>;
    for (const [k, v] of Object.entries(bundle)) {
      if (typeof v === "string") {
        expect(
          !/^TODO\b/i.test(v),
          `bundle "${loc}" key ${k} still has a TODO placeholder`,
        );
        expect(
          v.trim().length > 0,
          `bundle "${loc}" key ${k} is empty`,
        );
      }
    }
  }
}


function runLocaleLabelsPresent(): void {
  for (const loc of LOCALES) {
    expect(
      typeof LOCALE_LABEL[loc] === "string" &&
        LOCALE_LABEL[loc].length > 0,
      `LOCALE_LABEL["${loc}"] missing or empty`,
    );
  }
}


function runFunctionKeysAreCallable(): void {
  // ws_connections_count + chat_running_node are functions.
  for (const loc of LOCALES) {
    const bundle = MESSAGES[loc];
    const a = bundle.ws_connections_count(5);
    expect(typeof a === "string" && a.length > 0,
      `${loc}.ws_connections_count(5) returned empty`);
    const b = bundle.chat_running_node("planner");
    expect(typeof b === "string" && b.includes("planner"),
      `${loc}.chat_running_node should mention the node name`);
  }
}


export function main(): void {
  runBundleParity();
  runNoPlaceholderTodo();
  runLocaleLabelsPresent();
  runFunctionKeysAreCallable();
  // eslint-disable-next-line no-console
  console.log("i18n bundles: OK");
}


// Top-level execution so `tsx messages.test.ts` runs them.
if (typeof require !== "undefined" && require.main === module) {
  main();
}
