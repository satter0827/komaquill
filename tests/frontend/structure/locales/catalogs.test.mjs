import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

test('UI catalogs have matching keys, placeholders and error codes', () => {
  const en = JSON.parse(readFileSync('src/frontend/src/app/locales/en.json'))
  const ja = JSON.parse(readFileSync('src/frontend/src/app/locales/ja.json'))
  const placeholders = (text) =>
    [...text.matchAll(/\{([a-z_]+)\}/g)].map((match) => match[1]).sort()
  assert.deepEqual(Object.keys(en).sort(), Object.keys(ja).sort())
  for (const key of Object.keys(en).filter((key) => key !== 'errors'))
    assert.deepEqual(placeholders(en[key]), placeholders(ja[key]), key)
  assert.deepEqual(Object.keys(en.errors).sort(), Object.keys(ja.errors).sort())
})
