/**
 * Scoped config for the sandbox-activity unit tests.
 *
 * WHY IT EXISTS: `src/tests/setup.ts` installs a `window.localStorage` shim, and
 * it does so after probing `localStorage.setItem` on the global — which on a
 * Node build that exposes its own `localStorage` global throws before any test
 * is collected, taking the whole suite with it. That is a pre-existing failure
 * unrelated to these tests, and it hides every result behind it.
 *
 * These tests cover pure functions over plain objects: no DOM, no storage, no
 * setup. Running them under `node` with no setup file makes them measurable
 * under a broken global shim, and it is the same file `npm test` would run once
 * `setup.ts` is fixed.
 */
import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  test: {
    environment: "node",
    include: ["src/tests/sandbox-activity.test.ts"],
    setupFiles: [],
  },
});
