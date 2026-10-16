import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import vue from 'eslint-plugin-vue'

// Preserve the historical CC-8 source gate while the explicit gated P4 page
// consumes those endpoints. Endpoint declarations and tests carry the same tag.
const frozenGate = {
  meta: { type: 'problem', schema: [], messages: { gated: 'CC-8 endpoints require a frozen-gated: true source marker.' } },
  create(context) {
    const source = context.sourceCode
    const gated = source.getAllComments().some((comment) => /frozen-gated\s*:\s*true/.test(comment.value))
    return gated ? {} : {
      ImportSpecifier(node) {
        if (['ENDPOINT_DETECT_TEXT', 'ENDPOINT_EXTRACT'].includes(node.imported.name)) {
          context.report({ node, messageId: 'gated' })
        }
      },
    }
  },
}

export default [
  { ignores: ['dist/**', 'node_modules/**', 'e2e/**', '*.tsbuildinfo'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser, extraFileExtensions: ['.vue'] } },
  },
  {
    files: ['**/*.{ts,vue}'],
    rules: {
      'no-undef': 'off', // TypeScript checks browser / DOM names.
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    plugins: { 'frozen-gate': { rules: { 'no-frozen-endpoint-outside-gated': frozenGate } } },
    rules: { 'frozen-gate/no-frozen-endpoint-outside-gated': 'error' },
  },
]
