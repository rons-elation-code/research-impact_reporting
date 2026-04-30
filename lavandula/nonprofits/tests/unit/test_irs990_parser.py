"""Unit tests for irs990_parser.py (Spec 0026)."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from lavandula.nonprofits.irs990_parser import (
    ParseResult,
    Person,
    _clean_text,
    _derive_person_type,
    _is_truthy,
    parse_990_xml,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "990"
SAMPLES = Path(__file__).resolve().parents[4] / "sample_pdfs" / "990-test"


class TestParseOneontaCemetery:
    """AC27: Part VII Section A parsing with real fixture."""

    @pytest.fixture()
    def result(self) -> ParseResult:
        xml = (SAMPLES / "oneonta_cemetery_990.xml").read_bytes()
        return parse_990_xml(xml)

    def test_metadata(self, result: ParseResult):
        assert result.metadata.ein == "150406405"
        assert result.metadata.tax_period == "202312"
        assert result.metadata.is_amended is False
        assert result.metadata.return_ts is not None

    def test_people_count(self, result: ParseResult):
        assert len(result.people) == 10

    def test_officer_flags(self, result: ParseResult):
        patricia = next(p for p in result.people if p.person_name == "PATRICIA L ODELL")
        assert patricia.is_officer is True
        assert patricia.is_director is True
        assert patricia.person_type == "officer"
        assert patricia.title == "SECTY/TREASURER"

    def test_director_only(self, result: ParseResult):
        jean = next(p for p in result.people if p.person_name == "JEAN SIEMS")
        assert jean.is_director is True
        assert jean.is_officer is False
        assert jean.person_type == "director"

    def test_no_flags_listed(self, result: ParseResult):
        """AC49: Part VII entry with no role flags → person_type='listed'."""
        lawrence = next(p for p in result.people if p.person_name == "LAWRENCE MATTICE")
        assert lawrence.person_type == "listed"
        assert lawrence.is_officer is False
        assert lawrence.is_director is False

    def test_compensation_cents(self, result: ParseResult):
        """AC31: compensation stored in cents (IRS dollars × 100)."""
        patricia = next(p for p in result.people if p.person_name == "PATRICIA L ODELL")
        assert patricia.reportable_comp == 2030000  # $20,300 × 100

    def test_hours(self, result: ParseResult):
        patricia = next(p for p in result.people if p.person_name == "PATRICIA L ODELL")
        assert patricia.avg_hours_per_week == Decimal("15.00")

    def test_no_schedule_j(self, result: ParseResult):
        """AC45: Filing without Schedule J leaves all Schedule J columns NULL."""
        for p in result.people:
            assert p.base_comp is None
            assert p.bonus is None
            assert p.deferred_comp is None

    def test_no_contractors(self, result: ParseResult):
        contractors = [p for p in result.people if p.person_type == "contractor"]
        assert len(contractors) == 0

    def test_no_warnings(self, result: ParseResult):
        assert result.warnings == []


class TestParsePLTW:
    """AC27/28/46: Part VII A+B and Schedule J with real fixture."""

    @pytest.fixture()
    def result(self) -> ParseResult:
        xml = (SAMPLES / "project_lead_the_way_990.xml").read_bytes()
        return parse_990_xml(xml)

    def test_metadata_amended(self, result: ParseResult):
        assert result.metadata.ein == "364802935"
        assert result.metadata.is_amended is True

    def test_officers_and_directors(self, result: ParseResult):
        non_contractors = [p for p in result.people if p.person_type != "contractor"]
        assert len(non_contractors) == 20

    def test_contractors(self, result: ParseResult):
        """AC28: Part VII Section B contractor parsing."""
        contractors = [p for p in result.people if p.person_type == "contractor"]
        assert len(contractors) == 5

    def test_contractor_fields(self, result: ParseResult):
        """AC35: contractor with PersonNm parses correctly."""
        ku = next(
            p for p in result.people
            if p.person_name == "UNIV OF KANSAS CENTER FOR RESEARCH"
        )
        assert ku.person_type == "contractor"
        assert ku.services_desc == "SUPPORT SERVICES"
        assert ku.reportable_comp == 140069600  # $1,400,696 × 100
        assert ku.is_officer is False
        assert ku.avg_hours_per_week is None

    def test_former_officer(self, result: ParseResult):
        """AC37: is_former=TRUE with person_type='officer' for former officers."""
        vince = next(p for p in result.people if p.person_name == "VINCE BERTRAM ED D")
        assert vince.is_former is True
        assert vince.person_type == "officer"

    def test_key_employee(self, result: ParseResult):
        """AC33: person_type derivation from role flags."""
        kathleen = next(p for p in result.people if p.person_name == "KATHLEEN MOTE")
        assert kathleen.person_type == "key_employee"
        assert kathleen.is_key_employee is True

    def test_highest_compensated(self, result: ParseResult):
        david_g = next(p for p in result.people if p.person_name == "DAVID GREER")
        assert david_g.person_type == "highest_compensated"
        assert david_g.is_highest_comp is True

    def test_schedule_j_merged(self, result: ParseResult):
        """AC43/46: Schedule J compensation breakdown populated."""
        dimmett = next(p for p in result.people if p.person_name == "DAVID DIMMETT ED D")
        assert dimmett.base_comp == 42844300  # $428,443 × 100
        assert dimmett.bonus == 7100000  # $71,000 × 100
        assert dimmett.deferred_comp == 4630000  # $46,300 × 100
        assert dimmett.nontaxable_benefits == 2420700  # $24,207 × 100
        assert dimmett.total_comp_sch_j == 56995000  # $569,950 × 100

    def test_schedule_j_former(self, result: ParseResult):
        vince = next(p for p in result.people if p.person_name == "VINCE BERTRAM ED D")
        assert vince.base_comp == 39226500
        assert vince.deferred_comp == 1800000

    def test_contractor_no_schedule_j(self, result: ParseResult):
        """AC45: Contractors don't get Schedule J data."""
        contractors = [p for p in result.people if p.person_type == "contractor"]
        for c in contractors:
            assert c.base_comp is None
            assert c.bonus is None

    def test_title_ampersand(self, result: ParseResult):
        dimmett = next(p for p in result.people if p.person_name == "DAVID DIMMETT ED D")
        assert dimmett.title == "PRESIDENT & CEO"


class TestNoPartVII:
    """AC29: parse XML missing Part VII → empty result, no error."""

    def test_empty_people(self):
        xml = (FIXTURES / "no_part_vii.xml").read_bytes()
        result = parse_990_xml(xml)
        assert result.people == []
        assert result.metadata.ein == "999999999"


class TestMissingPersonNm:
    """AC53: Part VII entry missing PersonNm → row skipped with WARNING."""

    def test_skipped_with_warning(self):
        xml = (FIXTURES / "no_person_nm.xml").read_bytes()
        result = parse_990_xml(xml)
        assert len(result.people) == 1
        assert result.people[0].person_name == "VALID PERSON"
        assert any("missing PersonNm" in w for w in result.warnings)


class TestScheduleJMismatch:
    """AC44/47: Schedule J name mismatch → warning, no orphan row."""

    def test_mismatch_warning(self):
        xml = (FIXTURES / "schedule_j_mismatch.xml").read_bytes()
        result = parse_990_xml(xml)
        assert len(result.people) == 1
        alice = result.people[0]
        assert alice.person_name == "ALICE SMITH"
        assert alice.base_comp is None  # not merged
        assert any("COMPLETELY WRONG NAME" in w for w in result.warnings)
        assert any("ERROR:" in w for w in result.warnings)


class TestXXERejection:
    """AC34: XML parsing rejects DTDs and external entities."""

    def test_xxe_rejected(self):
        xml = (FIXTURES / "xxe_attack.xml").read_bytes()
        with pytest.raises(Exception):
            parse_990_xml(xml)


class TestBooleanIndicators:
    """AC12: boolean indicator variations."""

    def test_is_truthy_values(self):
        import defusedxml.ElementTree as ET

        for val in ("X", "x", "true", "TRUE", "1"):
            el = ET.fromstring(f"<Ind>{val}</Ind>")
            assert _is_truthy(el) is True, f"Expected truthy for {val!r}"

    def test_is_falsy(self):
        import defusedxml.ElementTree as ET

        for val in ("", "false", "0", "N"):
            el = ET.fromstring(f"<Ind>{val}</Ind>")
            assert _is_truthy(el) is False, f"Expected falsy for {val!r}"

    def test_none(self):
        assert _is_truthy(None) is False


class TestPersonTypeDerivation:
    """AC33: person_type priority derivation."""

    def test_officer_wins(self):
        assert _derive_person_type(True, True, True, True, False) == "officer"

    def test_key_employee_over_director(self):
        assert _derive_person_type(False, True, False, True, False) == "key_employee"

    def test_highest_comp_over_director(self):
        assert _derive_person_type(False, False, True, True, False) == "highest_compensated"

    def test_director_only(self):
        assert _derive_person_type(False, False, False, True, False) == "director"

    def test_no_flags_listed(self):
        """AC49: no role flags → person_type='listed'."""
        assert _derive_person_type(False, False, False, False, False) == "listed"

    def test_former_only(self):
        """AC37: is_former alone → person_type='officer'."""
        assert _derive_person_type(False, False, False, False, True) == "officer"


class TestHTMLStripping:
    """AC39: HTML tags stripped from text fields."""

    def test_strip_tags(self):
        assert _clean_text("John <b>Smith</b>") == "John Smith"

    def test_strip_complex_tags(self):
        assert _clean_text('<a href="x">Click</a> here') == "Click here"

    def test_whitespace_collapse(self):
        assert _clean_text("  John   Smith  ") == "John Smith"

    def test_none(self):
        assert _clean_text(None) is None

    def test_empty(self):
        assert _clean_text("") is None


class TestCompensationCents:
    """AC31/AC13: compensation in cents."""

    def test_whole_dollars_to_cents(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <Return xmlns="http://www.irs.gov/efile" returnVersion="2023v4.0">
          <ReturnHeader>
            <ReturnTs>2024-01-15T10:00:00-05:00</ReturnTs>
            <TaxPeriodEndDt>2023-12-31</TaxPeriodEndDt>
            <Filer><EIN>111111111</EIN></Filer>
          </ReturnHeader>
          <ReturnData>
            <IRS990>
              <Form990PartVIISectionAGrp>
                <PersonNm>TEST PERSON</PersonNm>
                <TitleTxt>CEO</TitleTxt>
                <OfficerInd>X</OfficerInd>
                <ReportableCompFromOrgAmt>150000</ReportableCompFromOrgAmt>
                <ReportableCompFromRltdOrgAmt>25000</ReportableCompFromRltdOrgAmt>
                <OtherCompensationAmt>10000</OtherCompensationAmt>
              </Form990PartVIISectionAGrp>
            </IRS990>
          </ReturnData>
        </Return>"""
        result = parse_990_xml(xml)
        p = result.people[0]
        assert p.reportable_comp == 15000000
        assert p.related_org_comp == 2500000
        assert p.other_comp == 1000000


class TestContractorNameVariants:
    """AC35: contractor with BusinessName vs PersonNm."""

    def test_business_name_contractor(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <Return xmlns="http://www.irs.gov/efile" returnVersion="2023v4.0">
          <ReturnHeader>
            <ReturnTs>2024-01-15T10:00:00-05:00</ReturnTs>
            <TaxPeriodEndDt>2023-12-31</TaxPeriodEndDt>
            <Filer><EIN>111111111</EIN></Filer>
          </ReturnHeader>
          <ReturnData>
            <IRS990>
              <ContractorCompensationGrp>
                <ContractorName>
                  <BusinessName>
                    <BusinessNameLine1Txt>Acme Design LLC</BusinessNameLine1Txt>
                  </BusinessName>
                </ContractorName>
                <ServicesDesc>Design services</ServicesDesc>
                <CompensationAmt>80000</CompensationAmt>
              </ContractorCompensationGrp>
              <ContractorCompensationGrp>
                <ContractorName>
                  <PersonNm>Jane Doe</PersonNm>
                </ContractorName>
                <ServicesDesc>Consulting</ServicesDesc>
                <CompensationAmt>50000</CompensationAmt>
              </ContractorCompensationGrp>
            </IRS990>
          </ReturnData>
        </Return>"""
        result = parse_990_xml(xml)
        assert len(result.people) == 2
        biz = next(p for p in result.people if p.person_name == "Acme Design LLC")
        assert biz.person_type == "contractor"
        assert biz.services_desc == "Design services"
        assert biz.reportable_comp == 8000000

        individual = next(p for p in result.people if p.person_name == "Jane Doe")
        assert individual.person_type == "contractor"
        assert individual.reportable_comp == 5000000
