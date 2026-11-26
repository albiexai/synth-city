const fs = require('node:fs')
const path = require('node:path')

function hasLifecycleProblem(step) {
  if (step.category === 'hook' || step.category === 'fixture') {
    if (step.error || !Number.isFinite(step.duration) || step.duration < 0) return true
  }
  return step.steps.some(hasLifecycleProblem)
}

function boundaryIndices(steps, title) {
  return steps.flatMap((step, index) =>
    step.category === 'hook' && step.title === title ? [index] : [])
}

module.exports = class JourneyPhaseReporter {
  constructor() {
    this.failures = []
  }

  onTestEnd(test, result) {
    if (result.status !== 'failed' || result.steps.some(hasLifecycleProblem)) return
    const steps = result.steps
    // The JSON reporter drops these hooks. Keep evidence only for a failure between clean boundaries.
    const before = boundaryIndices(steps, 'Before Hooks')
    const after = boundaryIndices(steps, 'After Hooks')
    const failed = steps.flatMap((step, index) => step.category === 'test.step' && step.error ? [index] : [])
    if (before.length !== 1 || after.length !== 1 || failed.length !== 1) return
    if (before[0] >= failed[0] || failed[0] >= after[0]) return
    this.failures.push({id: test.id, title: test.title, retry: result.retry, worker: result.workerIndex})
  }

  onEnd() {
    const output = process.env.E2E_JOURNEY_PHASES
    if (!output) throw new Error('E2E_JOURNEY_PHASES is required')
    fs.mkdirSync(path.dirname(output), {recursive: true})
    fs.writeFileSync(output, JSON.stringify({version: 1, failures: this.failures}) + '\n')
  }
}
