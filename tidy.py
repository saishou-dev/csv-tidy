#!/usr/bin/env python3
"""CSV の整形・重複チェック・表記ゆれの統一をドライラン付きで行う小さなツール。

標準ライブラリだけで動きます（Python 3.10 以上）。
元のファイルは書き換えません。--dry-run で変更予定の一覧だけを出し、
--out で指定した別のファイルに結果を書き出します。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 整形の部品

# 前後から取り除く空白（半角スペース・全角スペース・タブ・改行）
TRIM_CHARS = " 　\t\r\n"

# 全角英数字と、よく混ざる全角記号を半角へ
_FULLWIDTH_TO_HALFWIDTH = {
    **{code: code - 0xFEE0 for code in range(0xFF10, 0xFF1A)},  # ０-９
    **{code: code - 0xFEE0 for code in range(0xFF21, 0xFF3B)},  # Ａ-Ｚ
    **{code: code - 0xFEE0 for code in range(0xFF41, 0xFF5B)},  # ａ-ｚ
    0xFF20: ord("@"),  # ＠
    0xFF0E: ord("."),  # ．
    0xFF0D: ord("-"),  # －
    0xFF3F: ord("_"),  # ＿
    0xFF0B: ord("+"),  # ＋
}
# （）／：などの全角記号は日本語の文章でそのまま使うため、半角に直しません。

# 半角カナの連続（濁点・半濁点を含む）
_HALFWIDTH_KANA_RUN = re.compile(r"[｡-ﾟ]+")

# 電話番号として扱う列名の手がかり
_PHONE_HINTS = ("電話", "TEL", "tel", "Tel", "phone", "Phone", "携帯", "FAX", "fax")

# 電話番号から取り除く区切り文字
_PHONE_SEPARATORS = re.compile(
    r"[-‐-―ー－\s　.．()（）/／]"
)


def trim(value: str) -> str:
    """前後の空白（全角スペースを含む）を取り除く。"""
    return value.strip(TRIM_CHARS)


def to_halfwidth_alnum(value: str) -> str:
    """全角の英数字・記号を半角へ直す。"""
    return value.translate(_FULLWIDTH_TO_HALFWIDTH)


def to_fullwidth_kana(value: str) -> str:
    """半角カナを全角カナへ直す（濁点・半濁点は1文字にまとめる）。"""
    return _HALFWIDTH_KANA_RUN.sub(
        lambda m: unicodedata.normalize("NFKC", m.group(0)), value
    )


def normalize_cell(value: str) -> str:
    """1つのセルに ①前後空白の除去 ②全角英数字→半角 ③半角カナ→全角カナ を順に当てる。"""
    text = trim(value if value is not None else "")
    text = to_halfwidth_alnum(text)
    text = to_fullwidth_kana(text)
    return text


def normalize_phone(value: str) -> str:
    """電話番号をハイフンなしの数字だけにそろえる（先頭の + は残す）。"""
    text = normalize_cell(value)
    if not text:
        return ""
    plus = text.startswith("+")
    stripped = _PHONE_SEPARATORS.sub("", text)
    digits = re.sub(r"\D", "", stripped)
    if not digits:
        return text  # 数字が1つも無い場合は触らない（「なし」などの記入）
    return ("+" + digits) if plus else digits


def looks_like_phone_column(name: str) -> bool:
    """列名から電話番号の列かどうかを推測する。"""
    return any(hint in name for hint in _PHONE_HINTS)


# ---------------------------------------------------------------- 結果の型


@dataclass(frozen=True)
class Change:
    """1セルの変更。row_no はヘッダーを1行目とした CSV 上の行番号。"""

    row_no: int
    column: str
    before: str
    after: str


@dataclass(frozen=True)
class Dropped:
    """重複として出力から外す行。"""

    row_no: int
    reason: str
    kept_row_no: int
    value: str


@dataclass
class TidyResult:
    fieldnames: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    blanks: dict[str, int] = field(default_factory=dict)
    read_count: int = 0
    phone_columns: list[str] = field(default_factory=list)
    key_columns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 本体


def tidy_rows(
    fieldnames: list[str],
    rows: list[dict[str, str]],
    key_columns: list[str] | None = None,
    phone_columns: list[str] | None = None,
    auto_phone: bool = True,
    drop_key_duplicates: bool = True,
) -> TidyResult:
    """読み込んだ行を整形し、重複を落とし、空欄を数える。"""
    key_columns = [c for c in (key_columns or []) if c in fieldnames]
    phones = list(phone_columns or [])
    if auto_phone:
        phones += [c for c in fieldnames if looks_like_phone_column(c) and c not in phones]
    phones = [c for c in phones if c in fieldnames]

    result = TidyResult(
        fieldnames=list(fieldnames),
        read_count=len(rows),
        phone_columns=phones,
        key_columns=key_columns,
    )

    seen_rows: dict[tuple[str, ...], int] = {}
    seen_keys: dict[tuple[str, str], int] = {}

    for index, raw in enumerate(rows):
        row_no = index + 2  # ヘッダーが1行目
        cleaned: dict[str, str] = {}
        for column in fieldnames:
            before = raw.get(column) or ""
            after = normalize_phone(before) if column in phones else normalize_cell(before)
            if after != before:
                result.changes.append(Change(row_no, column, before, after))
            cleaned[column] = after

        signature = tuple(cleaned[c] for c in fieldnames)
        if signature in seen_rows:
            result.dropped.append(
                Dropped(row_no, "完全重複", seen_rows[signature], "")
            )
            continue

        duplicate_key = None
        for column in key_columns:
            value = cleaned[column]
            if value and (column, value) in seen_keys:
                duplicate_key = (column, value, seen_keys[(column, value)])
                break
        if duplicate_key and drop_key_duplicates:
            column, value, kept = duplicate_key
            result.dropped.append(Dropped(row_no, f"{column} の重複", kept, value))
            continue
        if duplicate_key:
            column, value, kept = duplicate_key
            result.dropped.append(
                Dropped(row_no, f"{column} の重複（出力には残す）", kept, value)
            )

        seen_rows[signature] = row_no
        for column in key_columns:
            value = cleaned[column]
            if value:
                seen_keys.setdefault((column, value), row_no)
        result.rows.append(cleaned)

    result.blanks = {
        column: sum(1 for row in result.rows if not row[column]) for column in fieldnames
    }
    return result


# ---------------------------------------------------------------- 表示


def format_report(result: TidyResult, dry_run: bool, out_path: Path | None) -> str:
    """ドライラン・実行の両方で使う結果一覧。"""
    lines: list[str] = []

    lines.append("== 変更予定（行番号 / 列 / 変更前 → 変更後）==")
    if result.changes:
        for change in result.changes:
            lines.append(
                f"  {change.row_no:>4} 行 / {change.column} : "
                f"「{change.before}」 → 「{change.after}」"
            )
    else:
        lines.append("  （変更はありません）")

    lines.append("")
    lines.append("== 重複として落ちる行 ==")
    if result.dropped:
        for drop in result.dropped:
            detail = f"（{drop.kept_row_no} 行目と同一）"
            if drop.value:
                detail = f"（{drop.kept_row_no} 行目と同じ「{drop.value}」）"
            lines.append(f"  {drop.row_no:>4} 行: {drop.reason}{detail}")
    else:
        lines.append("  （重複はありません）")

    lines.append("")
    lines.append("== 空欄の件数（出力後）==")
    blanks = {c: n for c, n in result.blanks.items() if n}
    if blanks:
        for column, count in blanks.items():
            lines.append(f"  {column}: {count} 件")
    else:
        lines.append("  （空欄はありません）")

    lines.append("")
    lines.append("== 集計 ==")
    lines.append(f"  読み込み {result.read_count} 行")
    lines.append(f"  出力 {len(result.rows)} 行")
    lines.append(f"  変更セル {len(result.changes)} 個")
    lines.append(f"  重複で除外 {len(result.dropped)} 行")
    if result.phone_columns:
        lines.append(f"  電話番号として統一した列: {', '.join(result.phone_columns)}")
    if result.key_columns:
        lines.append(f"  重複を見た列: {', '.join(result.key_columns)}")

    lines.append("")
    if dry_run:
        lines.append("※ ドライランのため、ファイルは1つも書き込んでいません。")
    elif out_path is not None:
        lines.append(f"※ 書き出しました: {out_path}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 入出力


def read_csv(path: Path, encoding: str) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SystemExit(f"エラー: 見出し行が読めません: {path}")
        fieldnames = [name if name is not None else "" for name in reader.fieldnames]
        rows = [{c: (row.get(c) or "") for c in fieldnames} for row in reader]
    return fieldnames, rows


def write_csv(
    path: Path, fieldnames: list[str], rows: list[dict[str, str]], encoding: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tidy.py",
        description="CSV の整形・重複チェック・表記ゆれの統一を、ドライラン付きで行います。",
    )
    parser.add_argument("input", help="読み込む CSV ファイル")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="変更予定の一覧だけを表示し、ファイルは書き込まない",
    )
    parser.add_argument("--out", help="整形後の CSV を書き出す先（元のファイルとは別名）")
    parser.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="列名",
        help="重複を見る列（例: --key メールアドレス）。複数回指定できます",
    )
    parser.add_argument(
        "--keep-key-duplicates",
        action="store_true",
        help="--key の重複を一覧に出すだけで、出力からは落とさない",
    )
    parser.add_argument(
        "--phone-col",
        action="append",
        default=[],
        metavar="列名",
        help="電話番号としてそろえる列（既定では列名から自動判定）",
    )
    parser.add_argument(
        "--no-auto-phone",
        action="store_true",
        help="電話番号の列の自動判定をやめる",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="CSV の文字コード（既定: utf-8-sig／Excel 由来の CSV は cp932）",
    )
    parser.add_argument("--log", metavar="PATH", help="結果一覧をテキストファイルにも保存する")
    parser.add_argument("--force", action="store_true", help="--out の既存ファイルを上書きする")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    source = Path(args.input)
    if not source.is_file():
        print(f"エラー: ファイルが見つかりません: {source}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.out:
        print(
            "エラー: --dry-run か --out のどちらかを指定してください"
            "（まず --dry-run で結果一覧をご確認ください）。",
            file=sys.stderr,
        )
        return 2

    out_path = Path(args.out) if args.out else None
    if out_path is not None:
        if out_path.resolve() == source.resolve():
            print("エラー: --out に元のファイルは指定できません（元ファイルは変更しません）。", file=sys.stderr)
            return 2
        if out_path.exists() and not args.force:
            print(f"エラー: すでにあります: {out_path}（上書きするには --force）", file=sys.stderr)
            return 2

    fieldnames, rows = read_csv(source, args.encoding)
    result = tidy_rows(
        fieldnames,
        rows,
        key_columns=args.key,
        phone_columns=args.phone_col,
        auto_phone=not args.no_auto_phone,
        drop_key_duplicates=not args.keep_key_duplicates,
    )

    if not args.dry_run and out_path is not None:
        write_csv(out_path, result.fieldnames, result.rows, args.encoding)

    report = format_report(result, dry_run=args.dry_run, out_path=out_path)
    print(report)

    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(report + "\n", encoding="utf-8")
        print(f"※ 作業ログを保存しました: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
