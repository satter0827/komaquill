import path from 'node:path'
import { readFile } from 'node:fs/promises'
import {
  classify,
  contained,
  hash,
  jsonFile,
  verifyBundle,
} from './evidence.mjs'

export default class Reporter {
  tests = []
  constructor(_options, environment = process.env) {
    this.environment = environment
  }
  onTestEnd(test, result) {
    const scenario = `scenarios/${hash(test.id).slice(0, 20)}-${result.retry}`
    this.tests.push({
      id: test.id,
      title: test.title,
      status: result.status,
      expected_status: test.expectedStatus,
      outcome: classify(
        result.status,
        test.expectedStatus,
        this.environment.KOMAQUILL_WEB_MODE,
      ),
      scenario: scenario + '/scenario.json',
    })
  }
  async onEnd(result) {
    const directory = this.environment.KOMAQUILL_WEB_EVIDENCE_DIR
    const run = JSON.parse(
      await readFile(path.join(directory, 'run.json'), 'utf8'),
    )
    const discovery = run.execution === 'discovery'
    for (const test of this.tests) {
      if (test.status === 'skipped') continue
      test.evidence = await verifyBundle(
        path.dirname(contained(directory, test.scenario)),
      )
    }
    const complete =
      this.tests.length > 0 && this.tests.every((test) => test.evidence?.valid)
    const status =
      !discovery && !complete && result.status === 'passed'
        ? 'failed'
        : result.status
    await jsonFile(path.join(directory, 'index.json'), {
      format_version: 1,
      run_id: run.id,
      run_file: 'run.json',
      runner_status: status,
      evidence_complete: discovery ? null : complete,
      execution: run.execution,
      review_questions: 'review-questions.json',
      quality_review: 'not_reviewed',
      counts: this.tests.reduce(
        (counts, test) => ({
          ...counts,
          [test.outcome]: (counts[test.outcome] ?? 0) + 1,
        }),
        {},
      ),
      scenarios: this.tests,
    })
    return { status }
  }
}
