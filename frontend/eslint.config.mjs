import tseslint from "typescript-eslint";

const eslintConfig = [
  { ignores: [".next/**", ".next-dev/**", "node_modules/**", "next-env.d.ts"] },
  ...tseslint.configs.recommended,
];

export default eslintConfig;
