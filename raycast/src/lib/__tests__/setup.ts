// Global vitest setup: extends `expect` with jest-dom matchers
// (toBeInTheDocument, toHaveTextContent, ...) for every test file, both the
// node-environment pure-logic tests and the jsdom-environment component tests
// (the matchers themselves are inert without a DOM, so loading them
// everywhere is harmless and keeps this file the one place that wires them).
import "@testing-library/jest-dom/vitest";
