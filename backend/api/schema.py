"""Dataset listing, ingestion, workflow, and schema routes."""

from fastapi import APIRouter, File, Form, Query, UploadFile

from backend.main import (
    StoredDatasetPreprocessRequest,
    WorkflowPreprocessRequest,
    dataset_rows as dataset_rows_handler,
    dataset_schema as dataset_schema_handler,
    datasets as datasets_handler,
    delete_dataset_table as delete_dataset_table_handler,
    get_preprocessing_rules as get_preprocessing_rules_handler,
    preprocess_stored_dataset as preprocess_stored_handler,
    start_upload_job as start_upload_job_handler,
    upload_dataset as upload_dataset_handler,
    upload_job_status as upload_job_status_handler,
    workflow_dataset_detail as workflow_dataset_detail_handler,
    workflow_datasets as workflow_datasets_handler,
    workflow_preprocess as workflow_preprocess_handler,
    workflow_run_detail as workflow_run_detail_handler,
    workflow_store_processed as workflow_store_processed_handler,
)

router = APIRouter()


@router.get("/api/datasets")
def datasets():
    return datasets_handler()


@router.get("/api/datasets/preprocessing-rules")
def get_preprocessing_rules():
    return get_preprocessing_rules_handler()


@router.get("/api/datasets/workflow")
def workflow_datasets():
    return workflow_datasets_handler()


@router.get("/api/datasets/workflow/{dataset_id}")
def workflow_dataset_detail(dataset_id: str):
    return workflow_dataset_detail_handler(dataset_id)


@router.post("/api/datasets/workflow/{dataset_id}/preprocess")
def workflow_preprocess(dataset_id: str, request: WorkflowPreprocessRequest):
    return workflow_preprocess_handler(dataset_id, request)


@router.get("/api/datasets/workflow/{dataset_id}/runs/{run_id}")
def workflow_run_detail(dataset_id: str, run_id: str):
    return workflow_run_detail_handler(dataset_id, run_id)


@router.post("/api/datasets/workflow/{dataset_id}/runs/{run_id}/store")
def workflow_store_processed(dataset_id: str, run_id: str):
    return workflow_store_processed_handler(dataset_id, run_id)


@router.post("/api/datasets/upload")
async def upload_dataset(file: UploadFile = File(...), dataset_identity: str | None = Form(default=None)):
    return await upload_dataset_handler(file, dataset_identity)


@router.post("/api/datasets/upload-jobs")
async def start_upload_job(file: UploadFile = File(...), dataset_identity: str | None = Form(default=None)):
    return await start_upload_job_handler(file, dataset_identity)


@router.get("/api/datasets/upload-jobs/{job_id}")
def upload_job_status(job_id: str):
    return upload_job_status_handler(job_id)


@router.post("/api/datasets/{table_name}/preprocess")
def preprocess_stored_dataset(
    table_name: str,
    request: StoredDatasetPreprocessRequest,
):
    return preprocess_stored_handler(table_name, request)


@router.get("/api/datasets/{table_name}/schema")
def dataset_schema(table_name: str):
    return dataset_schema_handler(table_name)


@router.delete("/api/datasets/{table_name}")
def delete_dataset_table(table_name: str):
    return delete_dataset_table_handler(table_name)


@router.get("/api/datasets/{table_name}/rows")
def dataset_rows(
    table_name: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    search: str = Query(default="", max_length=200),
):
    return dataset_rows_handler(table_name, offset=offset, limit=limit, search=search)
