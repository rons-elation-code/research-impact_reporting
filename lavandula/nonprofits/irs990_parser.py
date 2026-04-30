"""Pure-function parser for IRS 990 XML filings (Spec 0026).

Extracts Part VII Section A (officers/directors/key employees),
Part VII Section B (independent contractors), and Schedule J
(compensation detail) from 990 XML bytes.

No database or network calls — takes XML bytes, returns structured data.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import defusedxml.ElementTree as ET

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Person:
    person_name: str
    title: str | None
    person_type: str
    avg_hours_per_week: Decimal | None
    reportable_comp: int | None
    related_org_comp: int | None
    other_comp: int | None
    services_desc: str | None
    is_officer: bool
    is_director: bool
    is_key_employee: bool
    is_highest_comp: bool
    is_former: bool
    base_comp: int | None = None
    bonus: int | None = None
    other_reportable: int | None = None
    deferred_comp: int | None = None
    nontaxable_benefits: int | None = None
    total_comp_sch_j: int | None = None


@dataclass
class FilingMetadata:
    return_ts: datetime | None
    is_amended: bool
    ein: str
    tax_period: str


@dataclass
class ParseResult:
    metadata: FilingMetadata
    people: list[Person]
    warnings: list[str] = field(default_factory=list)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find(el: ET.Element, local: str) -> ET.Element | None:
    for child in el:
        if _local_name(child.tag) == local:
            return child
    return None


def _find_all(el: ET.Element, local: str) -> list[ET.Element]:
    return [child for child in el if _local_name(child.tag) == local]


def _find_deep(root: ET.Element, local: str) -> list[ET.Element]:
    results = []
    for el in root.iter():
        if _local_name(el.tag) == local:
            results.append(el)
    return results


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = _HTML_TAG_RE.sub("", value)
    value = " ".join(value.split())
    return value if value else None


def _is_truthy(el: ET.Element | None) -> bool:
    if el is None:
        return False
    t = (el.text or "").strip().upper()
    return t in ("X", "TRUE", "1")


def _dollars(el: ET.Element | None) -> int | None:
    """Parse IRS 990 amount field (whole dollars, no cents)."""
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        return None


def _decimal(el: ET.Element | None) -> Decimal | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _derive_person_type(
    is_officer: bool,
    is_key_employee: bool,
    is_highest_comp: bool,
    is_director: bool,
    is_former: bool,
) -> str:
    if is_officer:
        return "officer"
    if is_key_employee:
        return "key_employee"
    if is_highest_comp:
        return "highest_compensated"
    if is_director:
        return "director"
    # FormerOfcrDirectorTrusteeInd with no other role flags → 'officer'
    if is_former:
        return "officer"
    return "listed"


def _find_irs990(root: ET.Element) -> ET.Element | None:
    return_data = _find(root, "ReturnData")
    if return_data is None:
        return None
    return _find(return_data, "IRS990")


def _parse_metadata(root: ET.Element) -> FilingMetadata:
    header = _find(root, "ReturnHeader")
    ein = ""
    tax_period = ""
    return_ts = None
    is_amended = False

    if header is not None:
        return_ts_el = _find(header, "ReturnTs")
        if return_ts_el is not None and return_ts_el.text:
            try:
                return_ts = datetime.fromisoformat(return_ts_el.text.strip())
            except ValueError:
                pass

        filer = _find(header, "Filer")
        if filer is not None:
            ein_el = _find(filer, "EIN")
            ein = _text(ein_el) or ""

        tax_period_el = _find(header, "TaxPeriodEndDt")
        if tax_period_el is not None and tax_period_el.text:
            raw = tax_period_el.text.strip()
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d")
                tax_period = dt.strftime("%Y%m")
            except ValueError:
                tax_period = raw.replace("-", "")[:6]

    irs990 = _find_irs990(root)
    if irs990 is not None:
        amended_el = _find(irs990, "AmendedReturnInd")
        is_amended = _is_truthy(amended_el)

    return FilingMetadata(
        return_ts=return_ts,
        is_amended=is_amended,
        ein=ein,
        tax_period=tax_period,
    )


def _parse_part_vii_a(irs990: ET.Element, warnings: list[str]) -> list[Person]:
    people = []
    for grp in _find_all(irs990, "Form990PartVIISectionAGrp"):
        name_el = _find(grp, "PersonNm")
        raw_name = _text(name_el)
        if raw_name is None:
            warnings.append("Part VII entry missing PersonNm — skipped")
            continue

        person_name = _clean_text(raw_name) or ""
        title = _clean_text(_text(_find(grp, "TitleTxt")))

        is_officer = _is_truthy(_find(grp, "OfficerInd"))
        is_director = _is_truthy(_find(grp, "IndividualTrusteeOrDirectorInd"))
        is_key_employee = _is_truthy(_find(grp, "KeyEmployeeInd"))
        is_highest_comp = _is_truthy(_find(grp, "HighestCompensatedEmployeeInd"))
        is_former = _is_truthy(_find(grp, "FormerOfcrDirectorTrusteeInd"))

        person_type = _derive_person_type(
            is_officer, is_key_employee, is_highest_comp, is_director,
            is_former,
        )

        people.append(Person(
            person_name=person_name,
            title=title,
            person_type=person_type,
            avg_hours_per_week=_decimal(_find(grp, "AverageHoursPerWeekRt")),
            reportable_comp=_dollars(_find(grp, "ReportableCompFromOrgAmt")),
            related_org_comp=_dollars(_find(grp, "ReportableCompFromRltdOrgAmt")),
            other_comp=_dollars(_find(grp, "OtherCompensationAmt")),
            services_desc=None,
            is_officer=is_officer,
            is_director=is_director,
            is_key_employee=is_key_employee,
            is_highest_comp=is_highest_comp,
            is_former=is_former,
        ))

    return people


def _parse_part_vii_b(irs990: ET.Element, warnings: list[str]) -> list[Person]:
    contractors = []
    for grp in _find_all(irs990, "ContractorCompensationGrp"):
        contractor_name_el = _find(grp, "ContractorName")
        person_name = None
        if contractor_name_el is not None:
            biz = _find(contractor_name_el, "BusinessName")
            if biz is not None:
                line1 = _find(biz, "BusinessNameLine1Txt")
                person_name = _clean_text(_text(line1))
            if person_name is None:
                pnm = _find(contractor_name_el, "PersonNm")
                person_name = _clean_text(_text(pnm))

        if not person_name:
            warnings.append(
                "Contractor entry missing both BusinessNameLine1Txt and PersonNm — skipped"
            )
            continue

        contractors.append(Person(
            person_name=person_name,
            title=None,
            person_type="contractor",
            avg_hours_per_week=None,
            reportable_comp=_dollars(_find(grp, "CompensationAmt")),
            related_org_comp=None,
            other_comp=None,
            services_desc=_clean_text(_text(_find(grp, "ServicesDesc"))),
            is_officer=False,
            is_director=False,
            is_key_employee=False,
            is_highest_comp=False,
            is_former=False,
        ))

    return contractors


def _merge_schedule_j(
    root: ET.Element,
    people: list[Person],
    warnings: list[str],
) -> None:
    schedule_j_groups = _find_deep(root, "RltdOrgOfficerTrstKeyEmplGrp")
    if not schedule_j_groups:
        return

    name_to_person: dict[str, Person] = {}
    for p in people:
        if p.person_type != "contractor":
            name_to_person[p.person_name] = p

    matched = 0
    total = len(schedule_j_groups)

    for grp in schedule_j_groups:
        raw_name = _clean_text(_text(_find(grp, "PersonNm")))
        if raw_name is None:
            warnings.append("Schedule J entry missing PersonNm — skipped")
            continue

        person = name_to_person.get(raw_name)
        if person is None:
            warnings.append(
                f"Schedule J name {raw_name!r} not found in Part VII — skipped"
            )
            continue

        matched += 1
        person.base_comp = _dollars(_find(grp, "BaseCompensationFilingOrgAmt"))
        person.bonus = _dollars(_find(grp, "BonusFilingOrganizationAmount"))
        person.other_reportable = _dollars(_find(grp, "OtherCompensationFilingOrgAmt"))
        person.deferred_comp = _dollars(_find(grp, "DeferredCompensationFlngOrgAmt"))
        person.nontaxable_benefits = _dollars(_find(grp, "NontaxableBenefitsFilingOrgAmt"))
        person.total_comp_sch_j = _dollars(_find(grp, "TotalCompensationFilingOrgAmt"))

    if total > 0 and matched == 0:
        log.error(
            "All %d Schedule J entries failed to match Part VII names", total,
        )
        warnings.append(
            f"ERROR: All {total} Schedule J entries failed to match Part VII names"
        )


def parse_990_xml(xml_bytes: bytes) -> ParseResult:
    """Parse Part VII A, B, and Schedule J from a 990 XML filing."""
    root = ET.fromstring(xml_bytes)
    metadata = _parse_metadata(root)
    warnings: list[str] = []

    irs990 = _find_irs990(root)
    if irs990 is None:
        return ParseResult(metadata=metadata, people=[], warnings=warnings)

    people = _parse_part_vii_a(irs990, warnings)

    has_part_vii = len(people) > 0

    contractors = _parse_part_vii_b(irs990, warnings)
    people.extend(contractors)

    if has_part_vii:
        _merge_schedule_j(root, people, warnings)

    if not has_part_vii and not contractors:
        return ParseResult(metadata=metadata, people=[], warnings=warnings)

    return ParseResult(metadata=metadata, people=people, warnings=warnings)
