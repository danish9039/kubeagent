#!/usr/bin/env python3
"""
KubeAgent Demo: Intentionally Broken ML Pipeline
=================================================
This pipeline is designed to fail in 3 different ways to demonstrate
KubeAgent's failure detection and auto-fix capabilities.

Failure Types Injected:
1. OOM Kill: data_preprocessing component has 50Mi memory limit (needs ~800MB)
2. Wrong Image: model_training uses tensorflow:2.99.99-gpu (nonexistent tag)
3. Missing Env: model_evaluation requires MODEL_REGISTRY_BUCKET env var (not set)

KubeAgent will:
- Detect all 3 failures via KFP API + Kubernetes pod events
- Correlate with MLflow metrics showing accuracy degradation
- Generate YAML patches to fix each issue
- Open GitHub PRs with the fixes
- Write incident reports
"""

import os

from kfp import dsl
from kfp.dsl import component, pipeline
from kfp import Client
import mlflow


# ---------------------------------------------------------------------------
# Component 1: data_preprocessing — OOM failure
# ---------------------------------------------------------------------------
@dsl.component(
    base_image="python:3.9",
    packages_to_install=["numpy", "pandas"],
    # Intentionally tiny memory — will OOM kill
)
def data_preprocessing(dataset_size: int) -> str:
    import numpy as np
    # Allocate massive array to trigger OOM.
    # With 50Mi memory limit, allocating 500MB will OOM kill.
    data = np.random.rand(10000, 10000)  # ~800MB
    return f"processed_{dataset_size}_samples"


# ---------------------------------------------------------------------------
# Component 2: model_training — Wrong image tag failure
# ---------------------------------------------------------------------------
@dsl.component(
    base_image="tensorflow/tensorflow:2.99.99-gpu",  # NONEXISTENT TAG — will ImagePullBackOff
    packages_to_install=[]
)
def model_training(preprocessed_data: str, learning_rate: float) -> float:
    # This will never run due to ImagePullBackOff
    import tensorflow as tf
    model = tf.keras.Sequential([
        tf.keras.layers.Dense(128, activation="relu", input_shape=(784,)),
        tf.keras.layers.Dense(64, activation="relu"),
        tf.keras.layers.Dense(10, activation="softmax"),
    ])
    model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=learning_rate),
                  loss="sparse_categorical_crossentropy",
                  metrics=["accuracy"])
    return 0.85


# ---------------------------------------------------------------------------
# Component 3: model_evaluation — Missing env var failure
# ---------------------------------------------------------------------------
@dsl.component(
    base_image="python:3.9",
    packages_to_install=["boto3"]
)
def model_evaluation(model_accuracy: float) -> dict:
    import os
    # Crash because env vars are missing
    s3_bucket = os.environ["MODEL_REGISTRY_BUCKET"]  # NOT SET → KeyError
    mlflow_uri = os.environ["MLFLOW_TRACKING_URI"]   # NOT SET → KeyError
    return {"accuracy": model_accuracy, "bucket": s3_bucket}


# ---------------------------------------------------------------------------
# Pipeline definition
# ---------------------------------------------------------------------------
@dsl.pipeline(
    name="broken-ml-training-pipeline",
    description="Intentionally broken ML training pipeline for KubeAgent demo"
)
def broken_ml_pipeline(
    dataset_size: int = 10000,
    learning_rate: float = 0.001
):
    preprocess_task = data_preprocessing(dataset_size=dataset_size)
    preprocess_task.set_memory_limit("50Mi")  # INTENTIONAL OOM
    preprocess_task.set_cpu_limit("500m")

    train_task = model_training(
        preprocessed_data=preprocess_task.output,
        learning_rate=learning_rate
    )
    # Uses nonexistent image tag — ImagePullBackOff

    eval_task = model_evaluation(model_accuracy=train_task.output)
    # Will crash on missing env vars


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------
def submit_pipeline():
    """Submit the broken pipeline and log degrading MLflow metrics."""
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # 1. Log degrading MLflow metrics to simulate model regression
    try:
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
        mlflow.set_experiment("broken-ml-experiment")

        with mlflow.start_run(run_name="training-run-degraded") as run:
            # Simulate degrading accuracy over epochs
            for epoch in range(10):
                accuracy = 0.95 - (epoch * 0.08)   # degrading from 0.95 to 0.23
                loss = 0.1 + (epoch * 0.15)          # increasing loss
                mlflow.log_metric("accuracy", accuracy, step=epoch)
                mlflow.log_metric("loss", loss, step=epoch)
                mlflow.log_metric("val_accuracy", accuracy - 0.05, step=epoch)

            mlflow.log_param("learning_rate", 0.001)
            mlflow.log_param("batch_size", 32)
            mlflow.log_param("model_type", "broken-transformer")
            mlflow.set_tag("pipeline_name", "broken-ml-training-pipeline")
            mlflow.set_tag("failure_injected", "true")

            logger.info(f"MLflow run logged: {run.info.run_id}")
    except Exception as e:
        logger.warning(f"MLflow logging failed (continuing): {e}")

    # 2. Submit broken pipeline to KFP
    kfp_endpoint = os.getenv("KFP_ENDPOINT", "http://localhost:8888")
    client = Client(host=kfp_endpoint)

    # Compile pipeline
    from kfp import compiler
    pipeline_yaml = "/tmp/broken_pipeline.yaml"
    compiler.Compiler().compile(broken_ml_pipeline, pipeline_yaml)

    # Create experiment (or retrieve if it already exists)
    try:
        experiment = client.create_experiment("broken-ml-experiment")
    except Exception:
        experiment = client.get_experiment(experiment_name="broken-ml-experiment")

    # Submit run
    run = client.create_run_from_pipeline_package(
        pipeline_file=pipeline_yaml,
        arguments={"dataset_size": 10000, "learning_rate": 0.001},
        run_name="broken-training-run-demo",
        experiment_id=experiment.experiment_id
    )

    logger.info(f"Pipeline submitted. Run ID: {run.run_id}")
    logger.info("This run will fail with 3 intentional errors:")
    logger.info("  1. OOM Kill in data_preprocessing (50Mi memory limit)")
    logger.info("  2. ImagePullBackOff in model_training (nonexistent image tag)")
    logger.info("  3. Missing env var KeyError in model_evaluation")
    logger.info("KubeAgent will detect and auto-fix these failures!")

    return run.run_id


if __name__ == "__main__":
    run_id = submit_pipeline()
    print(f"Run ID: {run_id}")
