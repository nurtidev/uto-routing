"""
Unit tests for app/core/compatibility.py
Run with: pytest tests/test_compatibility.py -v
"""
import pytest

from app.core.compatibility import get_vehicle_skills, is_vehicle_compatible


class TestGetVehicleSkills:
    def test_cement_unit_gets_cementing_skills(self):
        skills = get_vehicle_skills("ЦА-320 В112ОР")
        assert "цементирование" in skills
        assert "тампонаж" in skills

    def test_acn_unit_gets_washing_skills(self):
        skills = get_vehicle_skills("АЦН-12 А045КМ")
        assert "промывка" in skills

    def test_bus_gets_transport_skills(self):
        skills = get_vehicle_skills("Вахтовка КАМАЗ 012AB")
        assert "транспортировка" in skills or "вахта" in skills

    def test_unknown_vehicle_returns_empty_skills(self):
        """Unrecognised vehicle → general-purpose, no restriction."""
        skills = get_vehicle_skills("Неизвестная техника XYZ")
        assert skills == []

    def test_empty_name_returns_empty(self):
        assert get_vehicle_skills("") == []

    def test_none_name_returns_empty(self):
        assert get_vehicle_skills(None) == []

    def test_case_insensitive_matching(self):
        """Pattern matching must be case-insensitive."""
        skills_upper = get_vehicle_skills("ЦА-320")
        skills_lower = get_vehicle_skills("ца-320")
        assert set(skills_upper) == set(skills_lower)

    def test_skills_are_lowercase(self):
        """All returned skills should be lowercase strings."""
        skills = get_vehicle_skills("ЦА-320 В112ОР")
        for s in skills:
            assert s == s.lower(), f"Skill '{s}' is not lowercase"

    def test_geophysics_vehicle(self):
        skills = get_vehicle_skills("ГФ-каротаж 001")
        assert "геофизика" in skills

    def test_lifter_vehicle_gets_repair_skills(self):
        skills = get_vehicle_skills("ПОДЪЁМНИК А-50 ОА004")
        assert "крс" in skills or "освоение" in skills or "ремонт" in skills


class TestIsVehicleCompatible:
    def test_compatible_when_task_type_none(self):
        assert is_vehicle_compatible("Неизвестная техника", None) is True

    def test_compatible_when_no_skills(self):
        """Vehicle with no recognised skills is universal."""
        assert is_vehicle_compatible("Неизвестная техника XYZ", "цементирование") is True

    def test_compatible_when_skill_matches(self):
        assert is_vehicle_compatible("ЦА-320 В112ОР", "цементирование") is True

    def test_incompatible_when_skill_mismatch(self):
        """Bus cannot do cementing."""
        assert is_vehicle_compatible("Вахтовка КАМАЗ 012AB", "цементирование") is False

    def test_empty_task_type_always_compatible(self):
        assert is_vehicle_compatible("ЦА-320 В112ОР", "") is True
