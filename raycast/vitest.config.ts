import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// engine.ts imports "@raycast/api", which ships only types — its runtime is
// supplied by Raycast's bundler, so it can't be imported under plain node.
// Alias it to a stub that provides controllable test doubles (prefs, Clipboard,
// getSelectedText) so the module imports and the pure helpers can be tested.
export default defineConfig({
  resolve: {
    alias: {
      "@raycast/api": fileURLToPath(
        new URL("./src/lib/__tests__/raycast-api.stub.ts", import.meta.url),
      ),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
