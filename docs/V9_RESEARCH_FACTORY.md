# V9_RESEARCH_FACTORY

Living document. Structure from spec §3, §75–§79. Implementation `NOT STARTED` at Phase 0; built in Phases 14–16. Separate from production execution.

## Self-improvement loop (§3)
OBSERVE → MEASURE → IDENTIFY WEAKNESS → FORM TESTABLE HYPOTHESIS → CREATE CONTROLLED EXPERIMENT → CREATE CHALLENGER → BACKTEST → OUT-OF-SAMPLE VALIDATION → ROBUSTNESS TESTING → SHADOW / PAPER TESTING → COMPARE WITH CHAMPION → PROMOTE OR REJECT → MONITOR → ROLL BACK IF REQUIRED.

Self-improvement MUST NOT mean: RECENT LOSS → AI RANDOMLY CHANGES PARAMETERS → DEPLOY TO PRODUCTION. More runtime generates more evidence, not more risk.

## Research agents (§75)
Research Director, Hypothesis Agent, Experiment Planner, Quant Validator, Coding Agent, Code Review Agent, Security Review Agent, Test Agent, Release Agent.

## Hypothesis output (§76)
Each hypothesis structured: hypothesis_id, problem, observation, hypothesis, expected mechanism, baseline, challenger, universe, horizon, dataset, metrics, promotion criteria, failure criteria, risks. No vague "maybe RSI should be changed."

## Experiment registry statuses (§77)
PROPOSED, APPROVED_FOR_RESEARCH, RUNNING, FAILED, REJECTED, VALIDATED, SHADOW, PROMOTED, ROLLED_BACK.

## Research memory (§78)
Structured relationships between experiments, strategies, features, ML models, AI models, prompts, decisions, positions, market regimes, failures, improvements. Numeric authoritative facts stay in structured data (vector memory is not authoritative portfolio state).

## Concept drift (§79)
Monitor feature distributions, probability calibration, win rate, expectancy, drawdown, strategy behavior, sentiment distributions, AI decision distributions. Drift triggers REVALIDATION, not AUTOMATIC RISK INCREASE.