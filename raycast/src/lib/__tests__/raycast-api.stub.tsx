// Stand-in for "@raycast/api" under vitest. The real package ships only types
// (its runtime is provided by Raycast's bundler), so it can't be imported in a
// plain-node/jsdom test. vitest.config.ts aliases "@raycast/api" to this file
// so every command/lib file imports cleanly; tests drive it via the mutable
// state and real (jsdom-renderable) components below.
//
// TypeScript itself does NOT see this file: `npm run typecheck` resolves
// "@raycast/api" to the real package's .d.ts files regardless of the vitest
// alias, so a source file's prop usage is checked against the real API. This
// stub only has to behave correctly at TEST RUNTIME — match the real
// component's callback shape (what `onChange`/`onSubmit`/`onAction` receive)
// close enough that a rendered form/list/menu is genuinely interactive under
// React Testing Library, not just "does it crash".

import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

export const mockPrefs: Record<string, string> = {
  daemonPort: "",
  backend: "default",
  translate: "default",
  pythonBin: "",
  engineScript: "",
  soxBin: "",
};

let clipboardText = "";
export function setClipboardText(t: string): void {
  clipboardText = t;
}

export function getPreferenceValues<T = Record<string, string>>(): T {
  return { ...mockPrefs } as T;
}

export const Clipboard = {
  readText: async (): Promise<string> => clipboardText,
  copy: async (text: string): Promise<void> => {
    clipboardText = text;
  },
  paste: async (): Promise<void> => undefined,
};

// ---- Navigation -------------------------------------------------------------
// useNavigation().push(<Element/>) is how PipelineForm/ResultView etc. show a
// follow-up screen. There's no real navigation stack under jsdom; a test that
// cares what got pushed reads `lastPushedElement` (reset it in beforeEach via
// `resetRaycastApiMocks()`).
export let lastPushedElement: ReactNode = null;
export function useNavigation(): {
  push: (el: ReactNode) => void;
  pop: () => void;
} {
  return {
    push: (el: ReactNode) => {
      lastPushedElement = el;
    },
    pop: () => undefined,
  };
}

// ---- Toast --------------------------------------------------------------
export const Toast = {
  Style: { Animated: "animated", Success: "success", Failure: "failure" },
};
export interface ToastHandle {
  style: string;
  title: string;
  message?: string;
  hide: () => Promise<void>;
}
export const toastHistory: ToastHandle[] = [];
export async function showToast(options: {
  style: string;
  title: string;
  message?: string;
}): Promise<ToastHandle> {
  const handle: ToastHandle = { ...options, hide: async () => undefined };
  toastHistory.push(handle);
  return handle;
}

export async function getSelectedText(): Promise<string> {
  return "";
}

export async function closeMainWindow(): Promise<void> {
  return undefined;
}

export async function popToRoot(): Promise<void> {
  return undefined;
}

export async function openExtensionPreferences(): Promise<void> {
  return undefined;
}

// launchCommand tracks what it was called with so a test can assert on it,
// without needing a real command to exist.
export const launchCommandCalls: unknown[] = [];
export async function launchCommand(options: unknown): Promise<void> {
  launchCommandCalls.push(options);
}

export const LaunchType = {
  UserInitiated: "userInitiated",
  Background: "background",
} as const;

export const Icon = new Proxy({} as Record<string, string>, {
  get: (_target, key: string) => key,
});
export const Color = new Proxy({} as Record<string, string>, {
  get: (_target, key: string) => key,
});

// ---- Action / ActionPanel ---------------------------------------------------
// Each renders a real <button>, labeled by its `title`, so tests find and
// click it with `screen.getByRole("button", { name: "..." })`.

interface ActionProps {
  title: string;
  onAction?: () => void;
  [key: string]: unknown;
}
function ActionBase({ title, onAction }: ActionProps) {
  return (
    <button type="button" onClick={onAction}>
      {title}
    </button>
  );
}

function ActionCopyToClipboard({
  title,
  content,
  onCopy,
}: {
  title: string;
  content: string;
  onCopy?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={() => {
        void Clipboard.copy(content).then(() => onCopy?.());
      }}
    >
      {title}
    </button>
  );
}

function ActionPaste({
  title,
  content,
  onPaste,
}: {
  title: string;
  content?: string;
  onPaste?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={() => {
        if (content !== undefined) void Clipboard.copy(content);
        onPaste?.();
      }}
    >
      {title}
    </button>
  );
}

function ActionSubmitForm({
  title,
  onSubmit,
}: {
  title: string;
  onSubmit?: (values: Record<string, unknown>) => void | Promise<void>;
}) {
  const ctx = useContext(FormValuesContext);
  return (
    <button
      type="button"
      onClick={() => {
        void onSubmit?.(ctx?.values ?? {});
      }}
    >
      {title}
    </button>
  );
}

function ActionPush({ title, target }: { title: string; target?: ReactNode }) {
  return (
    <button
      type="button"
      onClick={() => {
        lastPushedElement = target ?? null;
      }}
    >
      {title}
    </button>
  );
}

export const Action = Object.assign(ActionBase, {
  CopyToClipboard: ActionCopyToClipboard,
  Paste: ActionPaste,
  SubmitForm: ActionSubmitForm,
  Push: ActionPush,
});

function ActionPanelBase({ children }: { children?: ReactNode }) {
  return <div role="group">{children}</div>;
}
function ActionPanelSubmenu({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div role="group" aria-label={title}>
      {children}
    </div>
  );
}
export const ActionPanel = Object.assign(ActionPanelBase, {
  Submenu: ActionPanelSubmenu,
});

// ---- Form -------------------------------------------------------------------
// A minimal but REAL controlled-form implementation: every Form.* field
// registers its current value in FormValuesContext by `id`, so
// Action.SubmitForm's onSubmit receives the same {id: value, ...} shape
// Raycast's real Form gives it. Fields work whether the caller passes them as
// controlled (value+onChange) or uncontrolled (defaultValue only).

const FormValuesContext = createContext<{
  values: Record<string, unknown>;
  setValue: (id: string, value: unknown) => void;
} | null>(null);

function FormBase({
  children,
  actions,
}: {
  children?: ReactNode;
  actions?: ReactNode;
  isLoading?: boolean;
}) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const setValue = (id: string, value: unknown) =>
    setValues((prev) => (prev[id] === value ? prev : { ...prev, [id]: value }));
  return (
    <FormValuesContext.Provider value={{ values, setValue }}>
      <form>
        {children}
        {actions}
      </form>
    </FormValuesContext.Provider>
  );
}

function useRegisteredValue(id: string, controlled: unknown, initial: unknown) {
  const ctx = useContext(FormValuesContext);
  const current =
    controlled !== undefined ? controlled : (ctx?.values[id] ?? initial ?? "");
  useEffect(() => {
    ctx?.setValue(id, current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, current]);
  return { ctx, current };
}

function FormTextArea({
  id,
  title,
  placeholder,
  value,
  defaultValue,
  onChange,
}: {
  id: string;
  title?: string;
  placeholder?: string;
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
}) {
  const { ctx, current } = useRegisteredValue(id, value, defaultValue);
  return (
    <textarea
      aria-label={title ?? id}
      placeholder={placeholder}
      value={current as string}
      onChange={(e) => {
        onChange?.(e.target.value);
        ctx?.setValue(id, e.target.value);
      }}
    />
  );
}

function FormTextField(props: Parameters<typeof FormTextArea>[0]) {
  const { id, title, placeholder, value, defaultValue, onChange } = props;
  const { ctx, current } = useRegisteredValue(id, value, defaultValue);
  return (
    <input
      aria-label={title ?? id}
      placeholder={placeholder}
      value={current as string}
      onChange={(e) => {
        onChange?.(e.target.value);
        ctx?.setValue(id, e.target.value);
      }}
    />
  );
}

function FormDropdown({
  id,
  title,
  value,
  defaultValue,
  onChange,
  children,
}: {
  id: string;
  title?: string;
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  children?: ReactNode;
}) {
  const { ctx, current } = useRegisteredValue(id, value, defaultValue);
  return (
    <select
      aria-label={title ?? id}
      value={current as string}
      onChange={(e) => {
        onChange?.(e.target.value);
        ctx?.setValue(id, e.target.value);
      }}
    >
      {children}
    </select>
  );
}
function FormDropdownItem({ value, title }: { value: string; title: string }) {
  return <option value={value}>{title}</option>;
}
FormDropdown.Item = FormDropdownItem;

function FormDescription({ title, text }: { title?: string; text?: string }) {
  return (
    <p>
      {title}
      {text}
    </p>
  );
}

export const Form = Object.assign(FormBase, {
  TextArea: FormTextArea,
  TextField: FormTextField,
  Dropdown: FormDropdown,
  Description: FormDescription,
});

// ---- List ---------------------------------------------------------------
function ListBase({
  children,
  searchBarPlaceholder,
}: {
  children?: ReactNode;
  isLoading?: boolean;
  searchBarPlaceholder?: string;
  onSearchTextChange?: (text: string) => void;
}) {
  return (
    <div role="list" aria-label={searchBarPlaceholder}>
      {children}
    </div>
  );
}
function ListItem({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  [key: string]: unknown;
}) {
  return (
    <div role="listitem" aria-label={title}>
      <span>{title}</span>
      {subtitle ? <span>{subtitle}</span> : null}
      {actions}
    </div>
  );
}
function ListEmptyView({
  title,
  description,
}: {
  title?: string;
  description?: string;
}) {
  return (
    <div role="listitem">
      {title}
      {description}
    </div>
  );
}
export const List = Object.assign(ListBase, {
  Item: ListItem,
  EmptyView: ListEmptyView,
});

// ---- MenuBarExtra ---------------------------------------------------------
function MenuBarExtraBase({
  children,
}: {
  children?: ReactNode;
  icon?: unknown;
  isLoading?: boolean;
}) {
  return <div role="menu">{children}</div>;
}
function MenuBarExtraItem({
  title,
  subtitle,
  onAction,
}: {
  title: string;
  subtitle?: string;
  onAction?: () => void;
  [key: string]: unknown;
}) {
  return (
    <button type="button" onClick={onAction}>
      {title}
      {subtitle}
    </button>
  );
}
function MenuBarExtraSeparator() {
  return <hr />;
}
export const MenuBarExtra = Object.assign(MenuBarExtraBase, {
  Item: MenuBarExtraItem,
  Separator: MenuBarExtraSeparator,
});

// ---- Detail ---------------------------------------------------------------
export function Detail({
  markdown,
  actions,
}: {
  markdown?: string;
  isLoading?: boolean;
  actions?: ReactNode;
}) {
  return (
    <div>
      <div>{markdown}</div>
      {actions}
    </div>
  );
}

// ---- Reset helper for tests -------------------------------------------------
// Call from a `beforeEach` to get a clean slate between tests: resets
// preferences to defaults, clears the clipboard/toast/launchCommand/navigation
// tracking state above. Does NOT reset anything a test set up itself (e.g. a
// custom mockPrefs override) beyond these known keys.
export function resetRaycastApiMocks(): void {
  clipboardText = "";
  toastHistory.length = 0;
  launchCommandCalls.length = 0;
  lastPushedElement = null;
  mockPrefs.daemonPort = "";
  mockPrefs.backend = "default";
  mockPrefs.translate = "default";
  mockPrefs.pythonBin = "";
  mockPrefs.engineScript = "";
  mockPrefs.soxBin = "";
}
