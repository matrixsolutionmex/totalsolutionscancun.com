import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import update

ROOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from app.database.connection import SessionLocal
from app.models.lead import Lead
from app.models.user import User  # noqa: F401 - garante a tabela users no metadata
from app.services.import_service import import_lead_records

DEFAULT_EXPORT_ROOT = PROJECT_ROOT / "exports"
DEFAULT_BATCH_SIZE = 1000


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload):
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def latest_incremental_batch(export_root: Path) -> Path:
    candidates = sorted(
        export_root.glob("incremental_matrix_*/incremental_batch.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"Nenhum lote incremental encontrado em {export_root}"
        )
    return candidates[0]


def crm_total(session) -> int:
    return session.query(Lead.id).count()


def chunked(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def apply_updates(session, updates, batch_size: int):
    stats = {
        "requested": len(updates),
        "applied": 0,
        "processed": 0,
        "missing_target": 0,
        "skipped_empty_payload": 0,
        "field_updates": Counter(),
    }

    for group_number, group in enumerate(chunked(updates, batch_size), start=1):
        candidate_ids = [item["crm_lead_id"] for item in group]
        existing_ids = {
            lead_id
            for (lead_id,) in session.query(Lead.id)
            .filter(Lead.id.in_(candidate_ids))
            .all()
        }

        mappings = []
        for item in group:
            lead_id = item["crm_lead_id"]
            payload = dict(item.get("update_payload") or {})

            if lead_id not in existing_ids:
                stats["missing_target"] += 1
                continue

            if not payload:
                stats["skipped_empty_payload"] += 1
                continue

            payload["updated_at"] = datetime.utcnow()
            mappings.append((lead_id, payload))

            for field_name in payload:
                if field_name != "updated_at":
                    stats["field_updates"][field_name] += 1

        if not mappings:
            continue

        for lead_id, payload in mappings:
            result = session.execute(
                update(Lead)
                .where(Lead.id == lead_id)
                .values(**payload)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                stats["applied"] += result.rowcount

        session.commit()
        stats["processed"] += len(group)
        print(
            f"Updates: lote {group_number} concluído "
            f"({stats['processed']}/{stats['requested']} processados, "
            f"{stats['applied']} aplicados)",
            flush=True,
        )

    stats["field_updates"] = dict(stats["field_updates"])
    return stats


def build_report(batch_path: Path, batch_payload, current_total_now: int, mode: str, execute: bool):
    summary = batch_payload["summary"]
    return {
        "generated_at": datetime.now().isoformat(),
        "batch_file": str(batch_path),
        "mode": mode,
        "execute": execute,
        "compare_snapshot": {
            "matrix_total": summary["matrix_total"],
            "crm_total_at_compare": summary["crm_total"],
            "already_in_crm": summary["already_in_crm"],
            "already_in_crm_unchanged": summary["already_in_crm_unchanged"],
            "needs_update": summary["needs_update"],
            "new_records": summary["new_records"],
            "duplicates": summary["duplicates"],
            "invalid_records": summary["invalid_records"],
        },
        "crm_total_now": current_total_now,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Aplica com segurança um lote incremental no Total Solutions CRM. "
            "Por padrão roda em dry-run e não grava nada."
        )
    )
    parser.add_argument(
        "--batch-file",
        type=Path,
        help="Caminho para incremental_batch.json. Se omitido, usa o lote incremental mais recente.",
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=DEFAULT_EXPORT_ROOT,
        help="Diretório raiz dos lotes incrementais.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Tamanho do lote para inserts/updates.",
    )
    parser.add_argument(
        "--mode",
        choices=("all", "inserts", "updates"),
        default="all",
        help="Escolhe se aplica inserts, updates ou ambos.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Executa a gravação no CRM. Sem essa flag, roda apenas dry-run.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Permite aplicar mesmo se o total do CRM atual divergir do snapshot usado na comparação.",
    )
    args = parser.parse_args()

    batch_path = args.batch_file or latest_incremental_batch(args.export_root)
    batch_payload = load_json(batch_path)
    summary = batch_payload["summary"]
    inserts = batch_payload.get("inserts", [])
    updates = batch_payload.get("updates", [])

    with SessionLocal() as session:
        current_total_now = crm_total(session)

        report = build_report(
            batch_path=batch_path,
            batch_payload=batch_payload,
            current_total_now=current_total_now,
            mode=args.mode,
            execute=args.apply,
        )

        if current_total_now != summary["crm_total"] and not args.force:
            report["status"] = "blocked_crm_total_mismatch"
            report["message"] = (
                "O CRM atual não bate com o snapshot da comparação. "
                "Rode uma nova análise incremental ou use --force conscientemente."
            )
            report_path = batch_path.parent / f"apply_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            write_json(report_path, report)
            print(report["message"])
            print(f"CRM no snapshot: {summary['crm_total']}")
            print(f"CRM agora: {current_total_now}")
            print(f"Relatório salvo em: {report_path}")
            raise SystemExit(2)

        planned_inserts = len(inserts) if args.mode in {"all", "inserts"} else 0
        planned_updates = len(updates) if args.mode in {"all", "updates"} else 0

        report["planned"] = {
            "inserts": planned_inserts,
            "updates": planned_updates,
            "post_apply_crm_total_estimate": current_total_now + planned_inserts,
        }

        if not args.apply:
            report["status"] = "dry_run_ready"
            report_path = batch_path.parent / f"apply_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            write_json(report_path, report)
            print("Dry-run concluído. Nenhuma gravação foi feita.")
            print(f"Batch: {batch_path}")
            print(f"CRM no snapshot: {summary['crm_total']}")
            print(f"CRM agora: {current_total_now}")
            print(f"Inserts planejados: {planned_inserts}")
            print(f"Updates planejados: {planned_updates}")
            print(f"Relatório salvo em: {report_path}")
            return

        execution = {
            "started_at": datetime.now().isoformat(),
            "insert_stats": None,
            "update_stats": None,
        }

        if args.mode in {"all", "inserts"}:
            execution["insert_stats"] = import_lead_records(
                session,
                inserts,
                batch_size=args.batch_size,
            ).__dict__

        if args.mode in {"all", "updates"}:
            execution["update_stats"] = apply_updates(
                session,
                updates,
                batch_size=args.batch_size,
            )

        final_total = crm_total(session)
        execution["finished_at"] = datetime.now().isoformat()
        execution["crm_total_after"] = final_total

        report["status"] = "applied"
        report["execution"] = execution

        report_path = batch_path.parent / f"apply_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        write_json(report_path, report)

        print("Aplicação concluída.")
        print(f"Batch: {batch_path}")
        print(f"CRM antes: {current_total_now}")
        print(f"CRM depois: {final_total}")
        if execution["insert_stats"]:
            print(f"Inserts aplicados: {execution['insert_stats']['inserted']}")
            print(f"Inserts pulados por duplicidade: {execution['insert_stats']['skipped_duplicates']}")
        if execution["update_stats"]:
            print(f"Updates aplicados: {execution['update_stats']['applied']}")
            print(f"Updates sem alvo: {execution['update_stats']['missing_target']}")
        print(f"Relatório salvo em: {report_path}")


if __name__ == "__main__":
    main()
