// Generated from openapi/discovery-v1.yaml; do not edit.
import type { CapabilityReadiness } from './enums'

export type GenerationOperation = "rag.answer.cited" | "wiki.pages"

export interface GenerationCandidate {
  node_id: string
  state: "approved"
  descriptor_valid_until: string
  operation: GenerationOperation
  readiness: CapabilityReadiness
  accepting_admissions: boolean
  observed_at?: string
  valid_until?: string
}

export interface GenerationCandidates {
  items: Array<GenerationCandidate>
}
