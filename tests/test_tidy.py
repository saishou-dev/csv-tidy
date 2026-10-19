"""tidy.py のテスト（標準ライブラリの unittest のみ）。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tidy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HEADER = ["顧客ID", "会社名", "メールアドレス", "電話番号"]


def make_rows(*values: tuple[str, ...]) -> list[dict[str, str]]:
    return [dict(zip(HEADER, row)) for row in values]


class TestNormalizeCell(unittest.TestCase):
    def test_前後の空白を取り除く(self):
        self.assertEqual(tidy.normalize_cell("  サンプル商事  "), "サンプル商事")
        self.assertEqual(tidy.normalize_cell("　サンプル商事　"), "サンプル商事")
        self.assertEqual(tidy.normalize_cell("\tテスト\n"), "テスト")

    def test_語の間の空白は残す(self):
        self.assertEqual(tidy.normalize_cell(" テスト 太郎 "), "テスト 太郎")

    def test_全角の英数字を半角にする(self):
        self.assertEqual(tidy.normalize_cell("Ｃ００３"), "C003")
        self.assertEqual(
            tidy.normalize_cell("ｊｉｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ"), "jiro@example.com"
        )

    def test_半角カナを全角カナにする(self):
        self.assertEqual(tidy.normalize_cell("ｻﾝﾌﾞﾙ"), "サンブル")
        self.assertEqual(tidy.normalize_cell("ﾀﾞﾆｰ ｼﾞｭｳｲﾁ"), "ダニー ジュウイチ")
        self.assertEqual(tidy.normalize_cell("ﾊﾟﾝ"), "パン")

    def test_日本語の全角記号は変えない(self):
        self.assertEqual(
            tidy.normalize_cell("電話は未取得（推測で埋めない）"),
            "電話は未取得（推測で埋めない）",
        )

    def test_空欄は空文字のまま(self):
        self.assertEqual(tidy.normalize_cell(""), "")
        self.assertEqual(tidy.normalize_cell("   "), "")


class TestNormalizePhone(unittest.TestCase):
    def test_ハイフンと空白と括弧を取り除く(self):
        self.assertEqual(tidy.normalize_phone("03-0000-0011"), "0300000011")
        self.assertEqual(tidy.normalize_phone("(03) 0000 0016"), "0300000016")
        self.assertEqual(tidy.normalize_phone(" 090-0000-0033 "), "09000000033")

    def test_全角の数字と区切りをそろえる(self):
        self.assertEqual(tidy.normalize_phone("０３－００００－００１３"), "0300000013")
        self.assertEqual(tidy.normalize_phone("０３ー００００ー００１５"), "0300000015")

    def test_国番号の先頭のプラスは残す(self):
        self.assertEqual(tidy.normalize_phone("+81-90-0000-0024"), "+819000000024")

    def test_数字が無い記入は書き換えない(self):
        self.assertEqual(tidy.normalize_phone("なし"), "なし")
        self.assertEqual(tidy.normalize_phone(""), "")

    def test_列名から電話の列を推測する(self):
        self.assertTrue(tidy.looks_like_phone_column("電話番号"))
        self.assertTrue(tidy.looks_like_phone_column("TEL"))
        self.assertTrue(tidy.looks_like_phone_column("携帯電話"))
        self.assertFalse(tidy.looks_like_phone_column("顧客ID"))
        self.assertFalse(tidy.looks_like_phone_column("メールアドレス"))


class TestTidyRows(unittest.TestCase):
    def test_完全重複の行を1つにまとめる(self):
        rows = make_rows(
            ("C001", "サンプル商事", "a@example.com", "03-0000-0011"),
            ("C002", "ダミー電機", "b@example.com", "03-0000-0012"),
            ("C001", " サンプル商事 ", "a@example.com ", "0300000011"),
        )
        result = tidy.tidy_rows(HEADER, rows)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0].row_no, 4)
        self.assertEqual(result.dropped[0].reason, "完全重複")
        self.assertEqual(result.dropped[0].kept_row_no, 2)

    def test_指定列の重複を検出して落とす(self):
        rows = make_rows(
            ("C001", "サンプル商事", "a@example.com", "03-0000-0011"),
            ("C002", "ダミー電機", "a@example.com", "03-0000-0012"),
        )
        result = tidy.tidy_rows(HEADER, rows, key_columns=["メールアドレス"])
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0].reason, "メールアドレス の重複")
        self.assertEqual(result.dropped[0].value, "a@example.com")

    def test_指定列の重複を残す設定では出力が減らない(self):
        rows = make_rows(
            ("C001", "サンプル商事", "a@example.com", "03-0000-0011"),
            ("C002", "ダミー電機", "a@example.com", "03-0000-0012"),
        )
        result = tidy.tidy_rows(
            HEADER, rows, key_columns=["メールアドレス"], drop_key_duplicates=False
        )
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(len(result.dropped), 1)

    def test_空欄の件数を列ごとに数える(self):
        rows = make_rows(
            ("C001", "サンプル商事", "", "03-0000-0011"),
            ("C002", "", "", "   "),
        )
        result = tidy.tidy_rows(HEADER, rows)
        self.assertEqual(result.blanks["メールアドレス"], 2)
        self.assertEqual(result.blanks["会社名"], 1)
        self.assertEqual(result.blanks["電話番号"], 1)
        self.assertEqual(result.blanks["顧客ID"], 0)

    def test_変更したセルだけを記録する(self):
        rows = make_rows(("C001", " サンプル商事", "a@example.com", "0300000011"))
        result = tidy.tidy_rows(HEADER, rows)
        self.assertEqual(len(result.changes), 1)
        change = result.changes[0]
        self.assertEqual((change.row_no, change.column), (2, "会社名"))
        self.assertEqual((change.before, change.after), (" サンプル商事", "サンプル商事"))

    def test_電話の列を指定しなければ自動で判定する(self):
        rows = make_rows(("C001", "サンプル商事", "a@example.com", "03-0000-0011"))
        result = tidy.tidy_rows(HEADER, rows)
        self.assertEqual(result.phone_columns, ["電話番号"])
        result = tidy.tidy_rows(HEADER, rows, auto_phone=False)
        self.assertEqual(result.phone_columns, [])
        self.assertEqual(result.rows[0]["電話番号"], "03-0000-0011")


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "input.csv"
        with self.source.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(HEADER)
            writer.writerow(["Ｃ００１", " サンプル商事 ", "a@example.com", "03-0000-0011"])
            writer.writerow(["C001", "サンプル商事", "a@example.com", "0300000011"])
        self.before = self.source.read_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ドライランはファイルを書かない(self):
        code = tidy.main([str(self.source), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.source.read_bytes(), self.before)
        self.assertEqual(list(self.dir.iterdir()), [self.source])

    def test_outを付けると書き出して元ファイルは変わらない(self):
        out = self.dir / "out.csv"
        code = tidy.main([str(self.source), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.is_file())
        self.assertEqual(self.source.read_bytes(), self.before)
        with out.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["顧客ID"], "C001")

    def test_dry_runもoutも無いときは止まる(self):
        self.assertEqual(tidy.main([str(self.source)]), 2)

    def test_元ファイルと同じ名前のoutは断る(self):
        self.assertEqual(tidy.main([str(self.source), "--out", str(self.source)]), 2)
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_既存のoutはforceが無いと上書きしない(self):
        out = self.dir / "out.csv"
        out.write_text("残しておく", encoding="utf-8")
        self.assertEqual(tidy.main([str(self.source), "--out", str(out)]), 2)
        self.assertEqual(out.read_text(encoding="utf-8"), "残しておく")
        self.assertEqual(tidy.main([str(self.source), "--out", str(out), "--force"]), 0)

    def test_logで作業ログを保存する(self):
        log = self.dir / "log.txt"
        self.assertEqual(tidy.main([str(self.source), "--dry-run", "--log", str(log)]), 0)
        self.assertIn("== 集計 ==", log.read_text(encoding="utf-8"))


class TestSampleFiles(unittest.TestCase):
    def test_サンプルを整形するとexpected_outputと一致する(self):
        source = ROOT / "sample" / "input.csv"
        expected = ROOT / "sample" / "expected_output.csv"
        fieldnames, rows = tidy.read_csv(source, "utf-8-sig")
        result = tidy.tidy_rows(fieldnames, rows, key_columns=["メールアドレス"])
        with expected.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            expected_rows = [dict(row) for row in reader]
        self.assertEqual(result.rows, expected_rows)

    def test_サンプルの件数(self):
        source = ROOT / "sample" / "input.csv"
        fieldnames, rows = tidy.read_csv(source, "utf-8-sig")
        result = tidy.tidy_rows(fieldnames, rows, key_columns=["メールアドレス"])
        self.assertEqual(result.read_count, 32)
        self.assertEqual(len(result.rows), 28)
        self.assertEqual(len(result.dropped), 4)


if __name__ == "__main__":
    unittest.main()
