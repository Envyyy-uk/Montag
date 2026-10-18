"""Командний рядок: `python -m core check` та `python -m core init`."""

from __future__ import annotations

import argparse
import sys

from core.config import get_settings
from core.db import open_db
from core.system_check import format_report, run_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core", description="Автомонтаж: службові команди")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="перевірити FFmpeg, NVENC, CUDA")
    sub.add_parser("init", help="створити папки і базу даних")
    sub.add_parser("gemini-ping", help="один крихітний текстовий запит: перевірка ключа, моделі й ціни")
    sub.add_parser("tailscale-cert", help="HTTPS-сертифікат для <машина>.<tailnet>.ts.net (tailscale cert)")
    args = parser.parse_args(argv)

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "check":
        report = run_all()
        print(format_report(report))
        return 0 if report.ok else 1

    settings = get_settings()
    settings.ensure_dirs()
    if args.cmd == "gemini-ping":
        return gemini_ping(settings)
    if args.cmd == "tailscale-cert":
        from core import tailscale
        try:
            name = tailscale.make_cert(settings.resolve(settings.tls_cert_file), settings.resolve(settings.tls_key_file))
        except RuntimeError as exc:
            print(f"Не вдалося: {exc}")
            return 1
        print(f"Сертифікат готовий. Адреса для iPhone: https://{name}:{settings.https_port}/")
        print("Сертифікат діє 90 днів — повтори цю команду раз на 2 місяці.")
        return 0
    conn = open_db(settings.database_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    print(f"База: {settings.database_path} (схема v{version})")
    print(f"Бібліотека: {settings.library_path}")
    print(f"Inbox: {settings.inbox_path}")
    return 0


def gemini_ping(settings) -> int:
    from analysis.scoring import get_backend
    from core import budget

    backend = get_backend(settings)
    if backend is None:
        print("GEMINI_API_KEY не задано в .env")
        return 1
    res = backend.generate(settings.gemini_model, [{"role": "user", "parts": [{"text": "Відповідай одним словом: ок"}]}],
                           {"max_output_tokens": 20})
    if res.error:
        print(f"Помилка: {res.error}")
        return 1
    conn = open_db(settings.database_path)
    cost = budget.record_spend(conn, None, res.model_version or settings.gemini_model, "перевірка ключа",
                               res.usage, batch=False)
    st = budget.state(conn, settings.monthly_budget_usd, settings.daily_request_limit)
    conn.close()
    rates = budget.model_rates(res.model_version or settings.gemini_model)
    print(f"Відповідь: {res.text!r}")
    print(f"Модель: {res.model_version}  ціни з pricing.json: {rates}")
    print(f"Вартість запиту: ${cost:.6f}   за місяць: ${st.spent:.4f} / ${st.limit:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
