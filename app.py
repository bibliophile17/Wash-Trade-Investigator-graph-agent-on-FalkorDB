"""FastAPI server: JSON API + the single-page UI."""
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

import agent
import tools

app = FastAPI(title="Wash-Trade Investigator")
HERE = os.path.dirname(os.path.abspath(__file__))


class InvestigateRequest(BaseModel):
    token: str


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/tokens")
def tokens():
    return tools.token_overview()


@app.post("/api/investigate")
def investigate(req: InvestigateRequest):
    return agent.investigate(req.token)


@app.get("/api/case/{case_id}")
def case(case_id: str):
    return tools.case_graph(case_id)


@app.get("/api/cases")
def cases():
    return tools.list_cases()


@app.post("/api/reset")
def reset():
    return tools.reset_cases()
