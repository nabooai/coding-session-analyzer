import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/"] },
  {
    files: ["src/**/*.ts"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    rules: {
      // main.ts uses `cond ? resolve(x) : reject(err)` as a statement.
      "@typescript-eslint/no-unused-expressions": ["error", { allowTernary: true }],
    },
  },
);
