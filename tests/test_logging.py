"""Токены не попадают в логи (бот и админка). Проверяем в отдельном процессе: там не импортирован bot.py
(который раньше сам приглушал httpx) и логирование настраивается с нуля, как в admin.main()."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOT_TOKEN = "123456789:" + "Q" * 35       # ненастоящий токен в формате Telegram
CRM_TOKEN = "fake-crm-token-zz9"

SCRIPT = r'''
import asyncio, io, logging, sys
sys.path.insert(0, ROOT)
import httpx
import admin                                  # как процесс админки: bot.py не импортируем
assert "bot" not in sys.modules, "bot.py импортирован"
from logging_setup import add_secret, setup_logging

buf = io.StringIO()
setup_logging(logging.DEBUG, stream=buf, force=True)   # даже на DEBUG httpx не должен писать адреса
add_secret(BOT_TOKEN); add_secret(CRM_TOKEN)

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
# 1) запись httpx уровня INFO с токеном в адресе — как пишет сам httpx
logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', url)
# 2) настоящий httpx-запрос (без сети, MockTransport)
async def real():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        await c.post(url)
asyncio.run(real())
# 3) фильтр: токен в обычных записях, в args и в трейсбеке — вырезается
log = logging.getLogger("admin")
log.warning("ошибка запроса %s", url)
log.warning("CRM token is " + CRM_TOKEN)
try:
    raise RuntimeError("boom " + url)
except RuntimeError:
    log.exception("упало")
log.info("вход: /auth/tg?token=abcdefSECRETLINK123 и bare %s", BOT_TOKEN)
sys.stdout.write(buf.getvalue())
'''


class TestLogRedaction(unittest.TestCase):
    def test_tokens_never_reach_log(self):
        env = dict(os.environ, HOUSES_FILE=str(ROOT / "tests" / "fixtures" / "houses.json"))
        code = (f"ROOT={str(ROOT)!r}\nBOT_TOKEN={BOT_TOKEN!r}\nCRM_TOKEN={CRM_TOKEN!r}\n" + SCRIPT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        log = out.stdout
        self.assertNotIn(BOT_TOKEN, log)
        self.assertNotIn("Q" * 35, log)
        self.assertNotIn(CRM_TOKEN, log)
        self.assertNotIn("SECRETLINK", log)
        self.assertNotIn("HTTP Request", log)               # httpx приглушён
        self.assertIn("ошибка запроса https://api.telegram.org/bot<скрыто>/sendDocument", log)
        self.assertIn("CRM token is <скрыто>", log)
        self.assertIn("RuntimeError: boom https://api.telegram.org/bot<скрыто>", log)   # трейсбек тоже
        self.assertIn("/auth/tg?token=<скрыто>", log)

    def test_redact_function(self):
        sys.path.insert(0, str(ROOT))
        import logging_setup
        self.assertEqual(logging_setup.redact(f"x bot{BOT_TOKEN}/getMe"), "x bot<скрыто>/getMe")
        self.assertEqual(logging_setup.redact("обычный текст 12:30"), "обычный текст 12:30")


if __name__ == "__main__":
    unittest.main()
