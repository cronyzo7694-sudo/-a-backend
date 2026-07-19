"""Question Bank / Topic / Tag routes under ``/api/banks``."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.extensions import db
from app.models.bank import QuestionBank, Tag, Topic
from app.models.chapter import Chapter
from app.utils.decorators import roles_required
from app.utils.validators import parse_pagination, require_fields

banks_bp = Blueprint("banks", __name__)
logger = logging.getLogger("exam_os.routes.banks")


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (s or "tag")[:64]


@banks_bp.get("")
@jwt_required()
def list_banks():
    q = QuestionBank.query
    if request.args.get("active_only", "true").lower() == "true":
        q = q.filter_by(is_active=True)
    items = q.order_by(QuestionBank.name).all()
    return jsonify({"items": [b.to_dict() for b in items], "total": len(items)})


@banks_bp.post("")
@roles_required("admin")
def create_bank():
    data = _json_body()
    err = require_fields(data, ["name"])
    if err:
        return jsonify({"error": err}), 400
    name = str(data["name"]).strip()[:200]
    if QuestionBank.query.filter_by(name=name).first():
        return jsonify({"error": "Bank already exists"}), 409
    try:
        owner_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        owner_id = None
    bank = QuestionBank(
        name=name,
        code=(str(data.get("code") or "").strip()[:64] or None),
        description=data.get("description"),
        owner_id=owner_id,
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(bank)
    db.session.commit()
    return jsonify({"message": "Bank created", "item": bank.to_dict()}), 201


@banks_bp.put("/<int:bank_id>")
@roles_required("admin")
def update_bank(bank_id):
    bank = QuestionBank.query.get_or_404(bank_id)
    data = _json_body()
    if data.get("name"):
        bank.name = str(data["name"]).strip()[:200]
    if "code" in data:
        bank.code = (str(data.get("code") or "").strip()[:64] or None)
    if "description" in data:
        bank.description = data.get("description")
    if "is_active" in data:
        bank.is_active = bool(data["is_active"])
    db.session.commit()
    return jsonify({"message": "Bank updated", "item": bank.to_dict()})


@banks_bp.delete("/<int:bank_id>")
@roles_required("admin")
def delete_bank(bank_id):
    bank = QuestionBank.query.get_or_404(bank_id)
    db.session.delete(bank)
    db.session.commit()
    return jsonify({"message": "Bank deleted"})


@banks_bp.get("/topics")
@jwt_required()
def list_topics():
    q = Topic.query
    if request.args.get("chapter_id"):
        try:
            q = q.filter_by(chapter_id=int(request.args["chapter_id"]))
        except ValueError:
            return jsonify({"error": "Invalid chapter_id"}), 400
    items = q.order_by(Topic.order_index, Topic.name).all()
    return jsonify({"items": [t.to_dict() for t in items], "total": len(items)})


@banks_bp.post("/topics")
@roles_required("admin")
def create_topic():
    data = _json_body()
    err = require_fields(data, ["name"])
    if err:
        return jsonify({"error": err}), 400
    chapter_id = data.get("chapter_id")
    if chapter_id not in (None, ""):
        try:
            chapter_id = int(chapter_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid chapter_id"}), 400
        if not Chapter.query.get(chapter_id):
            return jsonify({"error": "Chapter not found"}), 404
    else:
        chapter_id = None
    parent_id = data.get("parent_id")
    if parent_id not in (None, ""):
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid parent_id"}), 400
    else:
        parent_id = None
    topic = Topic(
        name=str(data["name"]).strip()[:200],
        chapter_id=chapter_id,
        parent_id=parent_id,
        order_index=int(data.get("order_index") or 0),
    )
    db.session.add(topic)
    db.session.commit()
    return jsonify({"message": "Topic created", "item": topic.to_dict()}), 201


@banks_bp.get("/tags")
@jwt_required()
def list_tags():
    items = Tag.query.order_by(Tag.name).all()
    return jsonify({"items": [t.to_dict() for t in items], "total": len(items)})


@banks_bp.post("/tags")
@roles_required("admin")
def create_tag():
    data = _json_body()
    err = require_fields(data, ["name"])
    if err:
        return jsonify({"error": err}), 400
    name = str(data["name"]).strip()[:64]
    slug = _slugify(data.get("slug") or name)
    if Tag.query.filter((Tag.name == name) | (Tag.slug == slug)).first():
        return jsonify({"error": "Tag already exists"}), 409
    tag = Tag(name=name, slug=slug)
    db.session.add(tag)
    db.session.commit()
    return jsonify({"message": "Tag created", "item": tag.to_dict()}), 201
