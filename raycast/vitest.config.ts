import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// engine.ts imports "@raycast/api", which ships only types — its runtime is
// supplied by Raycast's bundler, so it can't be imported under plain node.
// Alias it to a stub that provides controllable test doubles (prefs,
// Clipboard, getSelectedText, and real jsdom-renderable Form/List/Action/...
// components) so both the pure helpers and the UI command components render
// under React Testing Library.
export default defineConfig({
  resolve: {
    alias: {
      "@raycast/api": fileURLToPath(
        new URL("./src/lib/__tests__/raycast-api.stub.tsx", import.meta.url),
      ),
    },
  },
  test: {
    // Pure-logic tests (*.test.ts) run under node — no DOM needed, faster.
    // Component tests (*.test.tsx) run under jsdom so React Testing Library
    // can render and query real markup. See src/lib/__tests__/setup.ts for
    // the jest-dom matchers those tests use.
    environment: "node",
    environmentMatchGlobs: [["src/**/*.test.tsx", "jsdom"]],
    setupFiles: ["./src/lib/__tests__/setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    // Per-function coverage for the repository quality gate (`vitest run
    // --coverage`); `all` reports files no test loads at 0% instead of hiding them.
    coverage: {
      provider: "v8",
      all: true,
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/__tests__/**"],
    },
  },
});
