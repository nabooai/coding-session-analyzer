import { describe, expect, it } from "vitest";
import type { ProviderInfo } from "./api";
import { KIND_FIELDS, kindLabel, kindNote, modelOptions, otherVendor, parseModels, providerLine, providerWhere } from "./models";

const p = (over: Partial<ProviderInfo>): ProviderInfo => ({
  id: "corp", name: "Corp", kind: "anthropic", kind_label: "Anthropic", endpoint: "", region: "", project: "",
  api_version: "", credential_set: true, models: ["claude-sonnet-4-5"], updated_at: null, ...over,
});

describe("AI Models", () => {
  it("asks each provider type for exactly what the server checks", () => {
    const required = (k: keyof typeof KIND_FIELDS) => KIND_FIELDS[k].filter((f) => !f.optional).map((f) => f.key).sort();
    expect(required("anthropic")).toEqual(["credential"]);
    expect(required("bedrock")).toEqual(["credential", "region"]);
    expect(required("vertex")).toEqual(["credential", "project", "region"]);
    expect(required("openai")).toEqual(["credential"]);
    expect(required("azure_openai")).toEqual(["credential", "endpoint"]);
  });
  it("says why a provider type cannot run on this image, and names the fix", () => {
    expect(kindNote("bedrock", ["bedrock", "vertex"])).toContain("--target with-claude-code");
    expect(kindNote("vertex", ["bedrock", "vertex"])).toContain("Claude Code");
    expect(kindNote("openai", ["bedrock", "vertex"])).toBe("");
    expect(kindNote("bedrock", [])).toBe("");
  });
  it("parses the models box", () => {
    expect(parseModels(" a, b\nc,,a \n")).toEqual(["a", "b", "c"]);
  });
  it("says where a provider sends calls", () => {
    expect(providerWhere(p({ endpoint: "https://acme.openai.azure.com" }))).toBe("acme.openai.azure.com");
    expect(providerWhere(p({ region: "us-east5", project: "proj" }))).toBe("us-east5 · proj");
  });
  it("names a provider on another vendor's OpenAI-compatible endpoint by where it calls, not as OpenAI", () => {
    const gemini = p({ name: "Gemini", kind: "openai", kind_label: "OpenAI", endpoint: "https://generativelanguage.googleapis.com/v1beta/openai" });
    expect(otherVendor(gemini)).toBe(true);
    expect(providerLine(gemini)).toBe("generativelanguage.googleapis.com");
    expect(kindLabel(gemini)).toBe("OpenAI-compatible");
    const openai = p({ kind: "openai", kind_label: "OpenAI", endpoint: "https://api.openai.com/v1" });
    expect(otherVendor(openai)).toBe(false);
    expect(providerLine(openai)).toBe("OpenAI · api.openai.com");
    expect(kindLabel(openai)).toBe("OpenAI");
    expect(kindLabel(p({ kind: "openai", endpoint: "" }))).toBe("OpenAI");
    expect(kindLabel(p({ kind: "azure_openai", endpoint: "https://x.openai.azure.com" }))).toBe("Azure OpenAI");
    expect(providerLine(p({ kind: "openai", kind_label: "OpenAI" }))).toBe("OpenAI");
    expect(providerLine(p({ kind: "azure_openai", kind_label: "Azure OpenAI", endpoint: "https://x.openai.azure.com" }))).toBe("Azure OpenAI · x.openai.azure.com");
  });
  it("offers the install's credential first and keeps a choice whose provider is gone", () => {
    const opts = modelOptions([p({ models: ["m1", "m2"] })], "gone/x", "sonnet", "an Anthropic API key");
    expect(opts.map((o) => o.value)).toEqual(["", "corp/m1", "corp/m2", "gone/x"]);
    expect(opts[0].label).toBe("sonnet · an Anthropic API key");
    expect(opts[3].label).toContain("provider removed");
  });
  it("with no install credential, lists only the models that can run", () => {
    const gemini = p({ id: "gem", name: "Gemini", kind: "openai", models: ["gemini-2.5-pro", "gemini-2.5-flash"] });
    const opts = modelOptions([gemini], "gem/gemini-2.5-pro", "sonnet", "");
    expect(opts.map((o) => o.label)).toEqual(["gemini-2.5-pro · Gemini", "gemini-2.5-flash · Gemini"]);
    expect(modelOptions([gemini], "", "haiku", "").map((o) => o.label)[0]).toBe("Not set");
  });
});
