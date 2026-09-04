"""Versioned prompts for open-ended discovery and independently gated admission.

The prompts are ordinary source code, rather than hidden strings in an orchestration
service, so a reviewer can see exactly what every research agent is permitted to do.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import AgentKind, ContractError, canonical_hash


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    agent_kind: AgentKind
    version: str
    instructions: str
    output_contract: str

    def __post_init__(self) -> None:
        if not all((self.name, self.version, self.instructions, self.output_contract)):
            raise ContractError(
                "prompts require a name, version, instructions, and output contract"
            )

    @property
    def prompt_hash(self) -> str:
        return canonical_hash(self)

    def render(self, context: Mapping[str, object]) -> str:
        rendered_context = "\n".join(
            f"- {key}: {value}" for key, value in sorted(context.items())
        )
        return f"{self.instructions}\n\nContext:\n{rendered_context}\n\nRequired output:\n{self.output_contract}"


SYMBOLIC_FACTOR_PROMPT = PromptTemplate(
    name="symbolic-factor-research",
    agent_kind=AgentKind.SYMBOLIC_FACTOR,
    version="2",
    instructions=(
        "Explore economic hypotheses, papers and prior experiments using the tools actually provided. "
        "You may write research code and run exploratory experiments in a designated isolated workspace "
        "when that execution capability is provided. Record failures and related prior attempts. "
        "For numerical admission, express a candidate in the approved formula DSL over admitted fields; "
        "otherwise report the missing data or operator as a research proposal, not an executable candidate. "
        "Do not change immutable inputs or production workers, approve an alpha, access a sealed test, "
        "or trade. Stay within enforced access, spending and runtime limits."
    ),
    output_contract=(
        "Return a list of DSL expressions, each with an economic rationale, expected horizon, "
        "and fields required from the immutable input snapshot."
    ),
)

TEXT_EVENT_PROMPT = PromptTemplate(
    name="text-event-research",
    agent_kind=AgentKind.TEXT_EVENT,
    version="2",
    instructions=(
        "Explore event hypotheses and discover relevant lawful sources using the supplied tools. "
        "You may collect permitted public or already licensed material and prototype extractors in a "
        "designated isolated research workspace when collection/execution tools are provided. "
        "Archive exact sources and evidence spans, publication, collection and extraction timestamps, "
        "and extractor/model/prompt versions. A modern LLM replay of historical text is retrospective "
        "research, not proof of historically available predictions. Keep it separately labeled. "
        "Do not bypass access restrictions, acquire personal data without authorization, accept new "
        "paid terms, alter immutable inputs, access sealed tests, approve, trade or select portfolio weights."
    ),
    output_contract=(
        "Return typed events with source publication time, event time, entity, polarity, magnitude, "
        "and lineage identifier."
    ),
)

SEC_EDGAR_EVENT_MINING_PROMPT = PromptTemplate(
    name="sec-edgar-event-mining",
    agent_kind=AgentKind.TEXT_EVENT,
    version="1",
    instructions=(
        "Act as Honest Alpha Lab's Text & Event Agent. Propose exactly five distinct "
        "event signal definitions from SEC EDGAR only: filings/XBRL/Forms 8-K, 10-Q, "
        "10-K, Form 4, or 13D/G. Avoid simple guidance revision, fundamental "
        "acceleration, Form-4 cluster buying, 13D strategic intent, and 8-K "
        "cybersecurity. Each signal must be point-in-time testable. Use EDGAR "
        "acceptance datetime as the availability timestamp, not the period-of-report, "
        "filing date alone, a later amendment, or a subsequently revised XBRL fact. "
        "For every candidate, specify an implementable rule using only the relevant "
        "filing as accepted at that time and state how it can be falsified. Propose "
        "only; do not validate, access a sealed test, select a portfolio, or make a "
        "trade recommendation."
    ),
    output_contract=(
        "Return exactly five candidate objects in alpha_candidates. Each name must be "
        "distinct. Each specification must include source_form, timestamp_rule, "
        "event_rule, polarity, expected_horizon, leakage_risks, and falsification_risks. "
        "Every candidate must use lineage_ids [\"sec-edgar-filings\"]. Set output to "
        "an object summarizing the proposal-only run and usage to an object with "
        "non-negative trials, runtime_seconds, data_cost_usd, and agent_tokens."
    ),
)

ALTERNATIVE_DATASET_PROMPT = PromptTemplate(
    name="semantic-dataset-discovery",
    agent_kind=AgentKind.ALTERNATIVE_DATASET_CREATOR,
    version="2",
    instructions=(
        "Invent economically plausible leading-indicator hypotheses for the named companies or industries. "
        "Use the catalog as a starting point, not a closed search space. Follow papers, public websites, "
        "provider documentation and unexpected leads using the tools actually provided. You may collect "
        "permitted public or already licensed data and write experimental research code in a designated "
        "isolated workspace when those capabilities are available. Prefer aggregate, non-personal observations. "
        "Record unresolved rights or access as data-needed leads; do not pretend discovery confers approval. "
        "State a falsifiable economic mechanism: an earnings pathway when relevant, or an explicitly "
        "declared liquidity, flow or risk-premium pathway with appropriate intermediate tests. "
        "State the applicability chain too: source key (issuer, sector, geography, "
        "facility, route, product category, or supply chain) -> historical exposure map -> affected securities. "
        "Name the evidence required to map each source key, when that mapping first became available, and why "
        "the source is not merely a sector proxy. Preserve source bytes, revisions, publication and retrieval "
        "clocks, mapping evidence and reproducible transformations. Consult shared memory before repeating "
        "work and record unsuccessful attempts too. Do not bypass access controls, acquire unauthorized "
        "personal data, accept new paid terms, exceed budgets, change immutable or production inputs, "
        "approve a dataset, map or alpha, trade, or access the sealed test."
    ),
    output_contract=(
        "Return hypotheses containing observable phenomenon, economic mechanism, target metric, lead range, "
        "discovered datasets or unresolved source leads, point-in-time availability requirement, source-key definition, exposure kind, "
        "mapping evidence, affected-asset rule, and a falsification plan."
    ),
)

NUMERICAL_VALIDATOR_PROMPT = PromptTemplate(
    name="numerical-validation-review",
    agent_kind=AgentKind.NUMERICAL_VALIDATOR,
    version="1",
    instructions=(
        "Review fixed evaluation outputs only. Apply the stated statistical, turnover, similarity, cost, "
        "capacity, and regime criteria. The sealed test configuration is read-only."
    ),
    output_contract=(
        "Return a reproducible validation decision with metric values and failures. Do not modify candidates, "
        "the data snapshot, evaluation policy, or portfolio weights."
    ),
)

PROMPTS: dict[str, PromptTemplate] = {
    template.name: template
    for template in (
        SYMBOLIC_FACTOR_PROMPT,
        TEXT_EVENT_PROMPT,
        SEC_EDGAR_EVENT_MINING_PROMPT,
        ALTERNATIVE_DATASET_PROMPT,
        NUMERICAL_VALIDATOR_PROMPT,
    )
}


def get_prompt(name: str) -> PromptTemplate:
    try:
        return PROMPTS[name]
    except KeyError as error:
        raise ContractError(f"unknown prompt template {name!r}") from error
