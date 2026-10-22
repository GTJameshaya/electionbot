"""Имена файла ведомости: понятное в чате/CRM («Дом_1_Сентябрь_2026.xlsx») и служебное
с отметкой времени в БД (по нему работает migrate_archive.py; повторные отправки не совпадают)."""
import asyncio
import os
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["HOUSES_FILE"] = str(ROOT / "tests" / "fixtures" / "houses.json")

import bot  # noqa: E402
import config  # noqa: E402
from captions import ascii_file_name, content_disposition, display_file_name, pretty_file_name, safe_file_part  # noqa: E402,E501
from db import ChatResend, Submission  # noqa: E402
from migrate_archive import FILE_RE  # noqa: E402
from test_offline import Base  # noqa: E402
from test_resend import CHAT, CRM, TAG, FakeCrm, ResendBase, token_of  # noqa: E402

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
ARCHIVE_RE = re.compile(r"^\d{8}_\d{6}_house\d+_(commerce_)?.+\.xlsx$")


def clock(*moments):
    """Подмена datetime в captions: now() по очереди возвращает moments (время отправки)."""
    seq = iter(moments)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(seq)
    return patch("captions.datetime", FakeDateTime)


class TestNameBuilder(unittest.TestCase):
    def test_residential_and_commerce(self):
        self.assertEqual(display_file_name(1, "2026-09"), "Дом_1_Сентябрь_2026.xlsx")
        self.assertEqual(display_file_name(2, "2026-09", "commerce"), "Дом_2_Коммерция_Сентябрь_2026.xlsx")
        self.assertEqual(pretty_file_name("Дом 1 к.2", "2026-09"), "Дом_1_к.2_Сентябрь_2026.xlsx")

    def test_all_months_nominative(self):
        for m, title in enumerate(MONTHS, 1):
            self.assertEqual(pretty_file_name("Дом 1", f"2025-{m:02d}"), f"Дом_1_{title}_2025.xlsx")
            self.assertEqual(pretty_file_name("Дом 1", f"2025-{m:02d}", "commerce"),
                             f"Дом_1_Коммерция_{title}_2025.xlsx")

    def test_forbidden_chars_and_spaces(self):
        cases = {
            'Дом\\1/2:3*4?5"6<7>8|9': "Дом_1_2_3_4_5_6_7_8_9",
            "ЖК «Тестовый»  дом   5": "ЖК_Тестовый_дом_5",
            "Дом\t1\n\x00\x1f\x7fкорпус": "Дом_1_корпус",
            "__..Дом 1..__": "Дом_1",
            "  Дом   1 ": "Дом_1",
            "Дом №1 ёлка": "Дом_№1_ёлка",                       # кириллица и № остаются
        }
        for raw, expected in cases.items():
            self.assertEqual(safe_file_part(raw), expected, raw)
            name = pretty_file_name(raw, "2026-01")
            self.assertEqual(name, f"{expected}_Январь_2026.xlsx", raw)
            self.assertIsNone(re.search(r'[\\/:*?"<>|«»\x00-\x1f\x7f\s]', name), raw)
            self.assertNotIn("__", name)
        self.assertEqual(pretty_file_name(" . ", "2026-02"), "Февраль_2026.xlsx")

    def test_period_not_send_date(self):
        # отправляют в ноябре за октябрь: в имени октябрь, а служебное имя — по дате отправки
        with clock(datetime(2026, 11, 3, 9, 15, 0)):
            from captions import archive_file_name
            arch = archive_file_name(1)
        self.assertEqual(display_file_name(1, "2026-10"), "Дом_1_Октябрь_2026.xlsx")
        self.assertTrue(arch.startswith("20261103_091500_house1_"))
        self.assertRegex(arch, FILE_RE)                      # migrate_archive.py его узнаёт


def parse_disposition(header: str) -> tuple[str, str]:
    """(имя, ASCII-запаска) как у браузеров по RFC 6266: filename* (RFC 5987) главнее filename."""
    from email.utils import decode_rfc2231
    from urllib.parse import unquote
    params = {}
    for part in header.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        params[key.lower()] = value
    charset, _, encoded = decode_rfc2231(params["filename*"])
    return unquote(encoded, encoding=charset), params["filename"].strip('"')


class TestContentDisposition(unittest.TestCase):
    def test_ascii_fallback(self):
        self.assertEqual(ascii_file_name("Дом_1_Сентябрь_2026.xlsx"), "Dom_1_Sentyabr_2026.xlsx")
        self.assertEqual(ascii_file_name("Дом_1_Коммерция_Май_2024.xlsx"), "Dom_1_Kommertsiya_May_2024.xlsx")
        self.assertEqual(ascii_file_name("Щёлково_№5_Январь_2026.xlsx"), "Shchelkovo_5_Yanvar_2026.xlsx")
        self.assertEqual(ascii_file_name("ЪЬ.xlsx"), "report.xlsx")
        for m in MONTHS:
            self.assertRegex(ascii_file_name(f"Дом_{m}.xlsx"), r"^[A-Za-z0-9._-]+\.xlsx$")

    def test_header_rfc5987(self):
        from urllib.parse import unquote
        name = "Дом_1_к.2_Коммерция_Сентябрь_2026.xlsx"
        h = content_disposition(name)
        h.encode("latin-1")                                   # заголовок HTTP — только ASCII
        self.assertTrue(h.isascii())
        self.assertTrue(h.startswith('attachment; filename="Dom_1_k.2_Kommertsiya_Sentyabr_2026.xlsx"; '))
        star = h.split("filename*=UTF-8''", 1)[1]
        self.assertRegex(star, r"^[A-Za-z0-9%._~-]+$")      # только attr-char и %XX
        self.assertEqual(unquote(star, encoding="utf-8"), name)
        self.assertEqual(parse_disposition(h), (name, "Dom_1_k.2_Kommertsiya_Sentyabr_2026.xlsx"))


class TestBotNames(Base):
    def ctx(self, crm=CRM):
        tg = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(message_id=777)),
                             username="fixture_bot")
        settings = config.Settings(bot_token="x", group_chat_id=CHAT, target_bot_tag=TAG, crm=crm)
        return SimpleNamespace(application=SimpleNamespace(bot_data={"db": self.db, "settings": settings}),
                               bot=tg), settings

    def send(self, ctx, settings, period, kind="residential"):
        async def go():
            await bot._send_ledger(ctx, settings, self.db, 1, period, kind, 5)
            await bot.wait_background(timeout=5)
        asyncio.run(go())

    def test_pretty_in_chat_and_crm_timestamped_in_db(self):
        ctx, settings = self.ctx()
        crm = FakeCrm()
        with crm.patch(), clock(datetime(2026, 11, 3, 9, 0, 0), datetime(2026, 11, 3, 9, 5, 0),
                                datetime(2026, 11, 4, 10, 0, 0)):
            self.send(ctx, settings, "2026-10")
            self.send(ctx, settings, "2026-10")                    # повторно за тот же месяц
            self.send(ctx, settings, "2026-10", "commerce")
        names = [c.kwargs["filename"] for c in ctx.bot.send_document.call_args_list]
        self.assertEqual(names, ["Дом_1_Октябрь_2026.xlsx", "Дом_1_Октябрь_2026.xlsx",
                                 "Дом_1_Коммерция_Октябрь_2026.xlsx"])
        self.assertEqual([crm.field(i, "files")[0] for i in range(3)], names)   # CRM — то же имя
        with self.db.Session() as s:
            stored = [x.file_name for x in s.query(Submission).order_by(Submission.id)]
        self.assertEqual(stored, ["20261103_090000_house1_Дом_1.xlsx", "20261103_090500_house1_Дом_1.xlsx",
                                  "20261104_100000_house1_commerce_Дом_1.xlsx"])
        self.assertEqual(len(set(stored)), 3)                  # повторная отправка не затирает прошлую
        for n in stored:
            self.assertRegex(n, FILE_RE)


class TestAdminNames(ResendBase):
    def test_download_uses_pretty_name(self):
        for kind, period, name in (("residential", "2024-05", "Дом_1_Май_2024.xlsx"),
                                   ("commerce", "2024-05", "Дом_1_Коммерция_Май_2024.xlsx"),
                                   ("residential", "2026-01", "Дом_1_Январь_2026.xlsx")):
            r = self.client.get(f"/download?house=1&kind={kind}&period={period}")
            self.assertEqual(r.status_code, 200)
            h = r.headers["content-disposition"]
            self.assertEqual(h, content_disposition(name))
            self.assertEqual(parse_disposition(h), (name, ascii_file_name(name)))
            self.assertIn(f'filename="{ascii_file_name(name)}"', h)

    def test_resend_pretty_in_chat_and_crm_timestamped_in_db(self):
        crm = FakeCrm()
        with crm.patch(), clock(datetime(2026, 11, 3, 9, 0, 0), datetime(2026, 11, 3, 9, 1, 0)):
            for _ in range(2):
                r = self.confirm(kind="commerce", period="2024-05", mode="ticket", ticket="new")
                self.send(token_of(r.text))
        self.assertEqual([c["filename"] for c in self.sender.calls],
                         ["Дом_1_Коммерция_Май_2024.xlsx"] * 2)
        self.assertEqual([crm.field(i, "files")[0] for i in range(2)], ["Дом_1_Коммерция_Май_2024.xlsx"] * 2)
        with self.db.Session() as s:
            resends = [x.file_name for x in s.query(ChatResend).order_by(ChatResend.id)]
            subs = [x.file_name for x in s.query(Submission).order_by(Submission.id)]
        expected = ["20261103_090000_house1_commerce_Дом_1.xlsx", "20261103_090100_house1_commerce_Дом_1.xlsx"]
        self.assertEqual(resends, expected)
        self.assertEqual(subs, expected)
        for n in expected:
            self.assertRegex(n, ARCHIVE_RE)
            self.assertRegex(n, FILE_RE)


if __name__ == "__main__":
    unittest.main()
