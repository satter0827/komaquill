import js from '@eslint/js'
import ts from 'typescript-eslint'
import hooks from 'eslint-plugin-react-hooks'
import { readdirSync } from 'node:fs'

const features = readdirSync(new URL('./src/features/', import.meta.url), {
  withFileTypes: true,
})
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
export default ts.config(
  { ignores: ['**/api/generated/**'] },
  js.configs.recommended,
  ...ts.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': hooks },
    rules: {
      ...hooks.configs.recommended.rules,
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  ...features.map((feature) => ({
    files: [`src/frontend/src/features/${feature}/**/*.{ts,tsx}`],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: `^(?:\\.\\./)+(?:app|${features.filter((name) => name !== feature).join('|')})(?:/|$)`,
              message:
                'Feature composition belongs in app; reusable modules belong in shared.',
            },
          ],
        },
      ],
    },
  })),
  {
    files: [
      'src/frontend/src/shared/**/*.{ts,tsx}',
      'src/frontend/src/api/**/*.{ts,tsx}',
    ],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              regex: '(?:^|/)(?:features/|app/(?!locales/(?:ja|en)\\.json$))',
              message:
                'Shared modules cannot depend on feature pages or application composition.',
            },
          ],
        },
      ],
    },
  },
)
