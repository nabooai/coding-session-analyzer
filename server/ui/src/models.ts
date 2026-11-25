// Pure logic for Admin > AI Models, kept out of main.ts so vitest can pin it: which fields each
// provider type asks for, how the models box parses, and the options each role can pick.
import type { ProviderInfo, ProviderKind } from "./api";

export type FieldKey = "credential" | "endpoint" | "region" | "project" | "api_version";

export interface KindField {
  key: FieldKey;
  label: string;
  placeholder: string;
  optional?: boolean;
  multiline?: boolean;       // a Vertex service-account key is a JSON file
}

export const KIND_FIELDS: Record<ProviderKind, KindField[]> = {
  anthropic: [
    { key: "credential", label: "API key", placeholder: "sk-ant-…" },
    { key: "endpoint", label: "Base URL", placeholder: "https://api.anthropic.com (or your gateway)", optional: true },
  ],
  bedrock: [
    { key: "region", label: "Region", placeholder: "eu-central-1" },
    { key: "credential", label: "Key", placeholder: "A Bedrock API key, or <access key id>:<secret access key>" },
  ],
  vertex: [
    { key: "project", label: "Project id", placeholder: "my-gcp-project" },
    { key: "region", label: "Region", placeholder: "us-east5" },
    { key: "credential", label: "Service account key", placeholder: "Paste the JSON key file", multiline: true },
  ],
  openai: [
    { key: "credential", label: "API key", placeholder: "sk-…" },
    { key: "endpoint", label: "Base URL", placeholder: "https://api.openai.com/v1", optional: true },
  ],
  azure_openai: [
    { key: "endpoint", label: "Resource endpoint", placeholder: "https://<resource>.openai.azure.com" },
    { key: "credential", label: "API key", placeholder: "Key 1 from the resource's Keys and Endpoint" },
    { key: "api_version", label: "API version", placeholder: "2024-10-21", optional: true },
  ],
};

// A provider's card on step 1 of the wizard: a lettermark badge (our own, no vendor logo), the
// label, and the host it talks to. Colours are brand-adjacent but defined here as our own tokens.
export interface ProviderMeta { label: string; host: string; mark: string; accent: string }
export const PROVIDER_META: Record<ProviderKind, ProviderMeta> = {
  anthropic: { label: "Anthropic", host: "api.anthropic.com", mark: "A", accent: "#d97757" },
  bedrock: { label: "AWS Bedrock", host: "bedrock, aws.amazon.com", mark: "B", accent: "#ff9900" },
  vertex: { label: "Google Vertex", host: "aiplatform, google", mark: "V", accent: "#4285f4" },
  openai: { label: "OpenAI", host: "api.openai.com", mark: "O", accent: "#10a37f" },
  azure_openai: { label: "Azure OpenAI", host: "openai.azure.com", mark: "Az", accent: "#0078d4" },
};

// The four wizard steps: pick a provider, connect and test, choose models, review.
export type WizardStep = "type" | "connection" | "models" | "review";
export const WIZARD_STEPS: { id: WizardStep; label: string; icon: string }[] = [
  { id: "type", label: "Provider", icon: "model" },
  { id: "connection", label: "Connection", icon: "lock" },
  { id: "models", label: "Models", icon: "lines" },
  { id: "review", label: "Review", icon: "doc" },
];

export const MODELS_HINT: Record<ProviderKind, string> = {
  anthropic: "claude-sonnet-4-5, claude-haiku-4-5",
  bedrock: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
  vertex: "claude-sonnet-4-5@20250929, claude-haiku-4-5@20251001",
  openai: "gpt-5, gpt-5-mini",
  azure_openai: "your deployment names, e.g. gpt-5-prod",
};

/** The models box: commas or new lines, blanks and repeats dropped, order kept. */
export function parseModels(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((m) => m.trim()).filter(Boolean))];
}

type Target = Pick<ProviderInfo, "kind" | "endpoint" | "region" | "project">;

/** Where a provider sends calls, for its table row: the endpoint's host, else the region. */
export function providerWhere(p: Omit<Target, "kind">): string {
  if (p.endpoint) {
    try {
      return new URL(p.endpoint).host;
    } catch {
      return p.endpoint;
    }
  }
  return [p.region, p.project].filter(Boolean).join(" · ");
}

/** An OpenAI-type provider pointed at another vendor's OpenAI-compatible endpoint (Gemini, a gateway):
 *  the admin's name for it says who it is, the adapter's name would not. */
export function otherVendor(p: Target): boolean {
  return p.kind === "openai" && !!p.endpoint && providerWhere(p) !== PROVIDER_META.openai.host;
}

/** A provider's type in words: "OpenAI-compatible" for `otherVendor`, else the type's own label. */
export function kindLabel(p: Target): string {
  return otherVendor(p) ? "OpenAI-compatible" : PROVIDER_META[p.kind].label;
}

/** The line under a provider's name: the type and where it sends calls, the type left out for `otherVendor`. */
export function providerLine(p: ProviderInfo): string {
  return [otherVendor(p) ? "" : p.kind_label, providerWhere(p)].filter(Boolean).join(" · ");
}

/** Why this image cannot run a provider type, for its card and its table row; "" when it can. */
export function kindNote(kind: ProviderKind, unavailable: ProviderKind[]): string {
  return unavailable.includes(kind) ? "Needs Claude Code: build the image with --target with-claude-code." : "";
}

export interface ModelOption { value: string; label: string }

/** Every model a role may run on: "" first when the install carries a Claude credential (`installed`,
 *  its name in words) or when nothing is picked yet, then each registered provider's models. A stored
 *  choice whose provider is gone stays listed, marked, so the page never shows a different model than
 *  the one in use, and never offers a default that has nothing to run on. */
export function modelOptions(providers: ProviderInfo[], current: string, envDefault: string, installed: string): ModelOption[] {
  const opts: ModelOption[] = installed ? [{ value: "", label: `${envDefault} · ${installed}` }]
    : current ? [] : [{ value: "", label: "Not set" }];
  for (const p of providers) for (const m of p.models) opts.push({ value: `${p.id}/${m}`, label: `${m} · ${p.name}` });
  if (current && !opts.some((o) => o.value === current)) opts.push({ value: current, label: `${current} (provider removed)` });
  return opts;
}
