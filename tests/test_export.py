from __future__ import annotations

from pathlib import Path
import unittest

from openpyxl import load_workbook

from frank_herz.core import Calibration, convert_sample, parse_data_line
from frank_herz.export import DATA_HEADERS, export_xlsx


class ExcelExportTests(unittest.TestCase):
    def test_export_is_valid_and_preserves_raw_measurements(self) -> None:
        calibration = Calibration()
        point = convert_sample(
            parse_data_line("DATA,123,1600,3200,0.100000,0.200000"),
            calibration,
        )
        directory = Path(__file__).resolve().parents[1] / ".test_tmp"
        directory.mkdir(exist_ok=True)
        path = directory / "franck_hertz.xlsx"
        try:
            self.assertEqual(export_xlsx(path, (point,), calibration), 1)
            workbook = load_workbook(path, data_only=True)
            sheet = workbook["Franck-Hertz Data"]
            self.assertEqual(tuple(cell.value for cell in sheet[1]), DATA_HEADERS)
            self.assertAlmostEqual(
                sheet["A2"].value,
                0.1 * calibration.drive_scale + calibration.drive_offset_v,
            )
            self.assertAlmostEqual(sheet["B2"].value, 200.0)
            self.assertEqual(sheet["C2"].value, 123)
            self.assertAlmostEqual(sheet["F2"].value, 1600.0)
            self.assertAlmostEqual(sheet["G2"].value, 3200.0)
            self.assertEqual(
                workbook["Calibration"]["B4"].value,
                calibration.picoammeter_mv_per_pa,
            )
            workbook.close()
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
