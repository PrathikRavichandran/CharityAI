from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
import os

def setup_telemetry(app_name: str, app=None):
    """
    Configures OpenTelemetry for a given service.
    If a FastAPI app is provided, it instruments the app.
    It unconditionally instruments all HTTPX clients so A2A requests get traced.
    """
    # 1. Provide Resource details (Service Name)
    resource = Resource.create({
        "service.name": app_name
    })

    # 2. Setup the Tracer Provider
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    # 3. Setup the OTLP Exporter (sending to Jaeger OTLP HTTP port 4318)
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    
    # 4. Add the exporter to the provider
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)

    # 5. Instrument FastAPI if provided
    if app:
        FastAPIInstrumentor.instrument_app(app)

    # 6. Instrument HTTPX globally (used by shared.a2a_client)
    # This automatically injects Tracecontext Headers into outward A2A requests.
    HTTPXClientInstrumentor().instrument()

    return provider
