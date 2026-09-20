// ESLint flat config — c4/agent（TypeScript）
// 聚焦正确性（类型感知规则），不启用激进风格规则。
// 运行：`npm run lint`（agent/ 目录），或 c4/ 根目录 `make lint`。
import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parserOptions: {
        projectService: {
          // 构建 tsconfig 排除测试目录（不进 dist）；lint 用 tsconfig.test.json
          // 覆盖 src + test 全量（类型感知规则对测试代码同样生效）
          defaultProject: "./tsconfig.test.json",
        },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // 异步密集型代码库：禁止未处理的悬浮 Promise
      "@typescript-eslint/no-floating-promises": "error",
      // 关闭 no-explicit-any：基线 14 处 `any` 均位于 MCP / LangChain / Express
      // 动态边界（消息载荷、运行时 API），正确收窄类型需先设计消息 schema，
      // 属行为设计决策，统一豁免（清单见静态检查基线报告）
      "@typescript-eslint/no-explicit-any": "off",
      // `_` 前缀 = 有意不使用的参数/变量（与 C 风格约定一致）
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          destructuredArrayIgnorePattern: "^_",
        },
      ],
    },
  },
);
