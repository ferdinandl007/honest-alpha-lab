# Automated alpha discovery: evidence and decisions for Honest Alpha Lab

Reviewed through **4 September 2026**. Research synthesis, not a trading recommendation or a report of discovered alpha.

## Bottom line

We should not rebuild a general quantitative research framework or assume that a larger swarm produces better signals. The best-supported direction in this review is a constrained, iterative search over executable hypotheses, with numerical feedback, a memory of successes and failures, and explicit measurement of what each new signal adds to an existing library.

Use Qlib/R&D-Agent as integration candidates; borrow AlphaBench's comparison structure; test evolutionary search and graph memory under equal budgets. Keep our independent evaluator, point-in-time controls, dataset-to-asset mapping and sealed test as non-negotiable boundaries. The alternative-dataset creation path deserves its own benchmark, not a footnote to formula mining.

Twenty works were screened, ranging from primary abstracts to methods and experimental sections. Reading depth and repository status are recorded in the [linked literature library](../research-vault/literature/index.md). This is a bounded review, not an exhaustive survey or proof of a universally best method. No paper's performance was reproduced; no external code was installed or run.

## What the evidence changes

| Decision | Evidence | Application |
| --- | --- | --- |
| Optimize the library, not the prettiest standalone backtest | [AlphaGen](https://arxiv.org/abs/2306.12964) | Measure marginal predictive and portfolio contribution against a frozen library snapshot. |
| Reuse research plumbing | [R&D-Agent-Quant](https://arxiv.org/html/2505.15155v2), [Qlib](https://github.com/microsoft/qlib) | Evaluate an adapter before building more orchestration infrastructure. Keep conventional models fixed initially. |
| Benchmark the search policy explicitly | [AlphaBench](https://alphabench.cc/) | Compare one-shot, feedback and evolutionary search; do not ask an LLM to choose winners by financial plausibility. |
| Store ancestry and useful failures | [AlphaPROBE](https://arxiv.org/html/2602.11917v1), [FactorMiner](https://arxiv.org/html/2602.14670v1) | Extend the vault into a retrieval graph, with duplicate and failure relationships—not just a collection of ideas. |
| Verify artifacts independently | [AgonAlpha](https://arxiv.org/html/2608.11250v1) | A fresh critic checks provenance and consistency; an authorized numerical service decides acceptance. |
| Give dataset discovery a distinct workflow | [AutoDataset](https://arxiv.org/html/2603.07271v1) | Discover sources from papers and official documentation, then separately assess rights, historical availability, asset mapping and incremental information. |

These are implementation recommendations inferred from the reviewed work, not claims that the combined design has already been validated.

## What to reuse, and what not to copy

**First spike: Qlib plus R&D-Agent's quant workflow.** The [R&D-Agent repository](https://github.com/microsoft/RD-Agent) contains actual implementation and displays an MIT license. Inspect a pinned version for compatibility with our typed CLI-job interface, artifact store and conventional model services. Do not import unrelated training functionality or accept its example datasets as proof of our universe requirements.

**Second spike: AlphaBench's task and baseline design.** Its [repository](https://github.com/CityU-MLO/AlphaBench) was inspected, but paper access was blocked by a browser challenge. The author project reports good generation relative to weak zero-shot financial judging, and favorable evolutionary-search results in its comparisons. This supports testing evolution—not declaring it the winner before our own experiment.

**Later ablations:** [QuantaAlpha](https://arxiv.org/html/2602.07085v1) motivates whole-trajectory mutation; [AlphaForge](https://arxiv.org/html/2406.18394v1) motivates separate adaptive combination. Test those only after the simpler loop is measurable. Jointly changing generation and combination makes attribution difficult.

**Do not copy phase-1-incompatible components.** AlphaGen's RL generator and Agora's PPO relay are outside scope. A conventional scheduler allocating research jobs is different from training an LLM or an RL alpha generator. No paper justifies allowing agents to change our final acceptance rules.

**Released code is not released evidence.** AgonAlpha's paper describes an extensive public trail, while its [repository](https://github.com/AutoResearch-Factory/AgonAlpha) explicitly excludes local candidates, results and evaluator integrations. We verified orchestration availability, not full reproduction. Its packaged workflow also needs adaptation; Codex compatibility has not been tested. Check licenses, dependencies, data rights and sandbox boundaries before reusing any repository.

## Strategy implications: what the next mining runs should investigate

These are proposed experiments, not validated strategies.

| Priority lane | Existing knowledge to reproduce | Incremental question for our agents | First falsification |
| --- | --- | --- | --- |
| Issuer-relative disclosure changes | [Lazy Prices](https://www.nber.org/papers/w25084) | Do structured changes in operating conditions or accounting warnings beat simple text differences? | Boilerplate, concurrent earnings, source timing and sector effects explain the result. |
| Accounting quality plus information response | Fixed, predeclared accounting factors | Does a weak immediate price response to a material accounting change add information beyond either component? | Equivalent to a known quality, momentum or reversal exposure. |
| Operating-demand proxy versus reported fundamentals | Existing retail/transport dataset hypotheses | Does a timestamp-correct proxy surprise predict operating outcomes, then residual returns beyond public filings and price data? | Revised observations, hindsight exposure maps, seasonality or contemporaneous public information explain it. |
| Economic exposure to supply/demand shocks | Existing energy, weather and freight hypotheses | Can dated facility/subsidiary/sector exposures identify which issuers are affected and in which direction? | Today's ownership is incorrectly projected backward, or the effect is only a broad sector return. |
| Complementary formula search | Fixed conventional factor library | Can constrained combinations add stable residual information after similarity and turnover controls? | Formula novelty disappears after sign-invariant correlation and library residualization. |

The [satellite-information study](https://pubsonline.informs.org/doi/10.1287/mnsc.2023.00713) provides a concrete example of operating observations, information access and returns being distinct objects. It does not validate our particular retail proxy or supply imagery.

Our dataset agent should propose a mechanism before collecting data. For fundamental proxies, the chain may be observation → operating activity → earnings surprise → price response. For liquidity, flows or risk-premium hypotheses, an earnings link is not necessarily the right test; specify the alternative mechanism and falsification criteria instead. Neither a persuasive story nor accurate sales forecasts alone establish abnormal returns.

The reviewed automated-mining evidence is concentrated in formulas and supplied data. AutoDataset supports source discovery as a separate engineering task, but does not establish an end-to-end alpha-producing dataset creator. This is a useful area to investigate—not a substantiated claim of global novelty.

## The shared research brain

Keep the immutable registry authoritative; use the Obsidian-compatible vault as a readable projection.

Proposed graph relationships:

- `derived_from`: parent hypothesis, formula and paper.
- `duplicates`: canonical expression match or sign-invariant score similarity.
- `uses_source`: source/version, availability rules, license decision and exposure-map version.
- `fails_because`: measured failure and its scope, not an agent's unsupported conclusion.
- `tested_in`: development snapshot, horizon, fold, sector and probabilistic state.
- `adds_to_library`: incremental result against a named frozen library snapshot.

Retrieve by mechanism and observed behavior, not only text embeddings. An unsuccessful idea can be worth revisiting with a genuinely new source, but that is a new logged trial with a specific reason. Keep strong negative results; never relabel them as untested.

[AlphaAgent](https://arxiv.org/html/2502.16789v1) supports structural originality checks, but semantic and numerical duplication need separate handling. For our adaptation, aggregate absolute pairwise correlations rather than taking an absolute value after averaging signed correlations: positive and negative duplicates must not cancel.

The added literature notes have stable IDs, reading-depth labels, source links and crosslinks to selected existing candidates. The JSON catalog is ready for a future retrieval adapter; that adapter is **not implemented** by this review.

## Proposed real-data benchmark

This is a protocol to implement, not an executed benchmark. Software fixtures may test correctness; they must never be reported as alpha evidence.

### Freeze the research environment

Use one licensed historical US equity snapshot with stable identifiers, eligible historical membership and documented delisting coverage. Pin all source versions, availability rules, model versions, prompts and evaluator code.

Specify the 5/10/20/60-trading-day residual-return labels exactly. Sector and market effects can overlap: define the residualization convention and estimate coefficients on training information only. Fix one primary horizon before search; count the others and all selected sectors, maps, regimes and thresholds in the research ledger.

Use the same library, DSL, complexity limits, costs and exposure controls across methods. Start with one fixed conventional predictor and an equal-weight combination. Compare ridge and LightGBM later under matched conditions.

### Search arms

| Arm | Purpose |
| --- | --- |
| Fixed known-factor library | Establish the non-agent reference portfolio. |
| Random type-correct formulas | Determine whether the DSL alone supplies useful candidates. |
| Classic genetic programming | Establish a conventional non-LLM search baseline. |
| Independent one-shot CLI proposals | Measure generation without adaptive feedback. |
| CLI proposals with numerical development feedback | Isolate iteration. |
| Evolutionary CLI search | Test parent selection, mutation and crossover. |
| Evolutionary search plus graph memory | Isolate ancestry/failure retrieval. |
| The same search plus independent critique | Measure additional verified evidence and reviewer cost. |

For a first feasibility pilot, propose three seeds and up to 30 expensive evaluation attempts per search arm per seed, with matched maximum model spend and measured runtime. This is a debugging/feasibility budget, not a statistically powered superiority study. Invalid generations, duplicates, failed evaluations and retries consume their declared budgets and remain visible; never grant a method unlimited free retries.

Compare quality at common evaluation budgets and common total-cost checkpoints. Fixed reference factors have no search spend but still incur data, evaluation and portfolio costs. Hold initial information and seed libraries constant; graph retrieval must not secretly give one arm additional accepted factors.

Run a separate dataset-discovery comparison: supplied curated sources versus a source-discovery agent with the same research objective and acquisition budget. Do not force a slow licensing/vintage task into the formula arm's throughput target.

### Measurements and rewards

Use hard eligibility gates before any scalar development reward: lawful use, reconstructible information timing, valid panel semantics, executable specification and independent evidence. Missing evidence is unknown or failed eligibility—not zero risk and not substituted with a stronger available metric.

Report separate measurements:

- Valid, economically distinct evaluated hypotheses per dollar and per hour.
- Development rank IC with dependence-aware uncertainty, plus change versus the fixed library/model.
- Out-of-sample net portfolio performance, turnover, drawdown, liquidity/capacity proxies and cost sensitivity.
- Stability across chronological folds, sectors and predeclared market conditions.
- Duplicate rate, source-to-feature success, mapping coverage/error audits, reviewer rejection and reproducibility rates.
- All attempted alternatives, including unsuccessful model, prompt, source and regime variants.

For scheduling, one possible **development-only** reward is a frozen, scaled combination of incremental predictive evidence and net-utility improvement, less similarity, turnover and acquisition/compute cost penalties. Choose scales and weights before the comparison; preserve the underlying vector. This is our proposed engineering choice, not a universal reward formula from the literature. Novelty alone receives no financial credit.

[AlphaEval](https://arxiv.org/html/2508.13174v1) motivates a cheap screening layer, not a replacement for historical execution. [Deflated Sharpe](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) and [PBO](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) inform selection-risk diagnostics. DSR applies to Sharpe inference, not arbitrary IC; dependent trials and overlapping labels need justified uncertainty treatment. No single statistic certifies discovery.

### Validation and market states

Search consumes only inner development feedback. Purge overlapping label intervals and apply the predeclared embargo around validation boundaries. Select policies within development; evaluate the frozen search procedure on outer chronological folds without adapting to their outcomes. Reserve a separate final period for one permissioned assessment of the selected pipeline and portfolio.

Fit HMM or clustering models on past data only. Use filtered state probabilities, not smoothed states informed by future observations. Compare state-aware conditioning with no-state and non-Markov alternatives. Report uncertainty when a state has few effective observations. The reviewed evidence does not establish HMM superiority or require a genuine signal to work in every state.

Do not tune a strategy after looking at its sealed result and continue calling that period sealed. Any subsequent adaptation needs new forward evidence.

## The important LLM-specific limitation

[Glasserman and Lin](https://arxiv.org/abs/2309.17322) and the [2026 financial-LLM bias review](https://arxiv.org/html/2602.14233v1) motivate an additional information-boundary audit. Historical documents can be correctly timestamped while a modern LLM knows later events. Current retrieval can also reveal later context.

Consequently:

- Archive original source bytes and evidence spans; forbid unbounded present-day retrieval in historical replay.
- Run entity-masking sensitivity checks, without treating masking as a guarantee.
- Separate retrospective exploratory results from prospective, frozen-model feature collection.
- Record when each research paper and dataset entered the agent's knowledge library. These September 2026 notes cannot be treated as contemporaneously available in a 2020 replay.

Independent external evaluation and chronological isolation address different failure modes; we need both. The [execution/reproducibility audit](https://arxiv.org/html/2606.08285v1) reinforces the importance of costs, universe and execution details. The newer [Agora study](https://arxiv.org/html/2606.29194v1) is worth watching, but its single full-system seed, short holdout and short-side concentration limit transfer to our portfolio objective.

## Implementation order

1. Resolve the correctness and authority gaps in the [existing project review](alpha-mining-review-2026-09-04.md): panel/rank semantics, missing-evidence rewards, durable sealed-test controls and portfolio valuation.
2. Obtain one real, reproducible historical snapshot and reproduce fixed baselines. Verify provider rights and availability separately from framework installation.
3. Complete proposal → deduplication → feature build → numerical development evaluation → reward → next proposal.
4. Run a small Qlib/R&D-Agent integration spike; benchmark the simple search arms before adding elaborate schedulers.
5. Connect the literature/candidate/failure graph; measure its incremental contribution.
6. Run a distinct lawful-source discovery and asset-mapping experiment for the alternative-data path.
7. Only then expand the candidate count, conventional portfolio combinations and forward shadow observation.

No mining, backtesting, provider connection, recurring automation, or production-policy change was performed in this research pass. The deliverables are the evidence synthesis, twenty linked notes and a machine-readable bibliography.

