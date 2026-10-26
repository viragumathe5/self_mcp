"""Stable, project-scoped source records shared by MCP and retrieval."""
import hashlib
import json


def task_results(store_id, runs):
    runs = sorted((run for run in runs if run["store_id"] == store_id),
                  key=lambda run: (run["updated_at"], run["id"]), reverse=True)
    results = []
    for run in runs:
        turns = [*run.get("turns", []), run]
        for index, turn in reversed(list(enumerate(turns))):
            turn_id = turn.get("turn_id", turn.get("id", run["id"]))
            base = {"store_id": store_id, "run_id": run["id"], "turn_id": turn_id,
                    "message_number": index + 1, "request": turn["goal"],
                    "is_current_message": turn is run, "run_status": run["status"], "message_status": turn["status"],
                    "saved_at": turn.get("updated_at", run["updated_at"])}

            def add(kind, title, content, *, task_id=None, version=1, latest=True,
                    review_status="unreviewed", provider=None, model=None):
                if not isinstance(content, str) or not content.strip():
                    return
                identity = json.dumps([store_id, run["id"], turn_id, kind, task_id, version, content], ensure_ascii=False)
                identifier = hashlib.sha256(identity.encode()).hexdigest()
                results.append({**base, "result_id": identifier, "uri": f"task-result://{store_id}/{identifier}",
                                "kind": kind, "title": title, "task_id": task_id, "version": version,
                                "is_latest_version": latest, "review_status": review_status,
                                "provider": provider, "model": model, "content": content})

            reviews = turn.get("reviews", [])
            approved = turn.get("review_approved")
            if approved is None and reviews:
                approved = reviews[-1].get("approved")
            add("final_answer", turn["goal"][:200], turn.get("final_output"),
                review_status="approved" if approved is True else "needs_review" if approved is False else "unreviewed",
                provider=turn.get("lead_provider") or turn.get("provider"), model=turn.get("lead_model"))
            for task in turn.get("tasks", []):
                versions = list(task.get("output_versions", []))
                if task.get("output") and not any(v.get("output") == task["output"] for v in versions):
                    versions.append({"version": task.get("output_version", len(versions) + 1), "output": task["output"]})
                latest_version = task.get("output_version", versions[-1].get("version", len(versions)) if versions else 0)
                for vi, version in reversed(list(enumerate(versions))):
                    number = version.get("version", vi + 1)
                    status = "unreviewed"
                    for review in reversed(reviews):
                        if review.get("output_versions", {}).get(task["id"]) != number:
                            continue
                        assessment = next((item for item in review.get("task_reviews", []) if item["task_id"] == task["id"]), None)
                        if assessment:
                            status = "kept" if assessment["verdict"] == "keep" else "revision_requested"
                            break
                    add("worker_draft", task["title"], version.get("output"), task_id=task["id"], version=number,
                        latest=number == latest_version, review_status=status,
                        provider=version.get("provider") or task.get("provider") or turn.get("worker_provider") or turn.get("provider"),
                        model=version.get("model") or task.get("model"))
            for ri, review in reversed(list(enumerate(reviews))):
                notes = {key: review[key] for key in ("approved", "feedback", "task_reviews", "decisions", "suggestions", "revision_tasks", "output_versions") if key in review}
                add("review_notes", f"Review {ri + 1}: {turn['goal'][:160]}", json.dumps(notes, ensure_ascii=False, indent=2),
                    version=ri + 1, latest=ri == len(reviews) - 1, review_status="model_judgment")
    return results
