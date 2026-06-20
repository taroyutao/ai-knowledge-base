import type { Plugin } from "@opencode-ai/plugin";

const TARGET_GLOB = "knowledge/articles/";
const TARGET_EXT = ".json";

function resolve_file_path(args: any): string | null {
  const raw = args?.filePath ?? args?.file_path ?? null;
  if (typeof raw !== "string" || raw.length === 0) return null;
  return raw;
}

function is_article_json(filePath: string): boolean {
  return filePath.includes(TARGET_GLOB) && filePath.endsWith(TARGET_EXT);
}

const plugin: Plugin = async (input) => {
  return {
    "tool.execute.after": async (hookInput, output) => {
      const { tool, args } = hookInput;
      if (tool !== "write" && tool !== "edit") return;

      const filePath = resolve_file_path(args);
      if (!filePath) return;

      if (!is_article_json(filePath)) return;

      try {
        const result = await input
          .$.nothrow()`python3 hooks/validate_json.py ${filePath}`;
        if (result.exitCode !== 0) {
          const errText = result.stderr?.toString() ?? "";
          output.metadata = {
            ...output.metadata,
            validation: {
              passed: false,
              exitCode: result.exitCode,
              error: errText.trim() || result.stdout?.toString()?.trim() || "",
            },
          };
          output.output +=
            `\n\n⚠️  知识条目校验失败 (exit=${result.exitCode})\n${errText}`;
        } else {
          output.metadata = {
            ...output.metadata,
            validation: { passed: true },
          };
        }
      } catch (err) {
        // Shell execution errors (process crashes, etc.) — log but don't block
      }
    },
    dispose: async () => {},
  };
};

export default plugin;
