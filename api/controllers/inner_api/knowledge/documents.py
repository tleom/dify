"""Read-only indexed document pages for an active, account-owned workbench run."""

from flask_restx import Resource
from pydantic import ValidationError
from sqlalchemy.orm import Session

from controllers.common.schema import register_response_schema_models, register_schema_models
from controllers.console.app.wraps import with_session
from controllers.inner_api import inner_api_ns
from controllers.inner_api.knowledge.retrieval import InnerKnowledgeRetrievalHttpError
from controllers.inner_api.wraps import plugin_inner_api_only
from services.entities.knowledge_documents import KnowledgeDocumentsPayload, KnowledgeDocumentsResponse
from services.workbench.knowledge_documents import read_documents

register_schema_models(inner_api_ns, KnowledgeDocumentsPayload)
register_response_schema_models(inner_api_ns, KnowledgeDocumentsResponse)


@inner_api_ns.route("/knowledge/documents")
class InnerKnowledgeDocumentsApi(Resource):
    @plugin_inner_api_only
    @inner_api_ns.doc("inner_knowledge_documents")
    @inner_api_ns.expect(inner_api_ns.models[KnowledgeDocumentsPayload.__name__])
    @inner_api_ns.response(200, "Indexed document page", inner_api_ns.models[KnowledgeDocumentsResponse.__name__])
    @with_session
    def post(self, session: Session) -> dict[str, object]:
        try:
            payload = KnowledgeDocumentsPayload.model_validate(inner_api_ns.payload or {})
        except ValidationError as exc:
            raise InnerKnowledgeRetrievalHttpError(
                error_code="invalid_request", description=str(exc), status_code=400
            ) from exc
        return read_documents(session, payload).model_dump(mode="json")
