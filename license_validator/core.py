from fastapi import FastAPI, Header, HTTPException, Request, Depends, APIRouter
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from datetime import datetime
from typing import List, Dict
import os
import redis.asyncio as redis
import httpx
import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = os.environ.get("REDIS_PORT")

DJANGO_API_URL = os.environ.get("DJANGO_API_URL")
DJANGO_API_USER = os.environ.get("DJANGO_API_USER")
DJANGO_API_PASSWORD = os.environ.get("DJANGO_API_PASSWORD")
SYNC_KEY = os.environ.get("SYNC_KEY")

app = FastAPI()
app.state.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
security = HTTPBasic()

licnese_router = APIRouter()

#Cache (Startup and shutdown handlers)
async def startup_event(app):
    app.state.redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    await initialize_admin_data()

async def shutdown_event(app):
    await app.state.redis.close()


#Admin panel
async def get_jwt_token():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{DJANGO_API_URL}/api/authorization/",
            data={"username": DJANGO_API_USER, "password": DJANGO_API_PASSWORD},
        )
        response.raise_for_status()
        return response.json()["access"]

async def fetch_all_admin_data():
    token = await get_jwt_token()
    headers = {"Authorization": f"Bearer {token}"}
    licenses_data = []
    parameters_data = []

    async with httpx.AsyncClient() as client:
        
        licenses_response = await client.get(f"{DJANGO_API_URL}/api/webhook/valid-licenses/", headers=headers)
        licenses_response.raise_for_status()
        licenses_data = licenses_response.json()
        
        parameters_response = await client.get(f"{DJANGO_API_URL}/api/webhook/parameters/", headers=headers)
        parameters_response.raise_for_status()
        parameters_data = parameters_response.json()

    return {"licenses": licenses_data, "parameters": parameters_data}


#Sync all licenses from admin panel to Redis
async def sync_redis_data(body):
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    await redis_client.flushdb()

    # === Sync Licenses ===
    licenses = body.get("licenses", [])
    license_keys = set()
    
    for lic in licenses:
        mod = lic.get("module_name")
        sn = lic.get("serial_num")
        cc = lic.get("company_code")
        to_date = lic.get("to_date")
        if not (mod and sn and cc):
            continue

        key = f"license:{mod}:{sn}:{cc}"
        license_keys.add(key)
        await redis_client.setex(key, 86400 if to_date else 3600, to_date or "")

    # === Sync Parameters ===
    parameters = body.get("parameters", [])
    param_keys = set()

    for param in parameters:
        mod = param.get("module_name")
        sn = param.get("serial_num")
        cc = param.get("company_code")
        if not (mod and sn and cc):
            continue

        key = f"params:{mod}:{sn}:{cc}"
        param_keys.add(key)
        await redis_client.set(key, json.dumps(param))

    response = {
        "status": "sync complete",
        "total_licenses": len(license_keys),
        "total_parameters": len(param_keys),
        "license_keys": list(license_keys),
        "param_keys": list(param_keys),
    }
    
    #print(f"Sync response: {json.dumps(response)}")
    
    return response

#Initialize licenses
async def initialize_admin_data():
    
    all_data = await fetch_all_admin_data()
    
    #print(f"Fetched data: {json.dumps(all_data)}")
    
    sync_result = await sync_redis_data(all_data)
    
    return sync_result

#Get client's Redis data
async def clients_cached_data(serial_num: str, company_code: str, module_name: str):
    redis_client = app.state.redis

    # License validation logic
    license_key = f"license:{module_name}:{serial_num}:{company_code}"
    license_status = "invalid"
    license_expiry = await redis_client.get(license_key)
    if license_expiry:
        if license_expiry == "":
            license_status = "invalid"
        else:
            try:
                expiry_date = datetime.fromisoformat(license_expiry).date()
                license_status = "valid" if datetime.utcnow().date() <= expiry_date else "expired"
            except ValueError:
                license_status = "invalid"

    # Parameter retrieval logic
    param_key = f"params:{module_name}:{serial_num}:{company_code}"
    param_data = await redis_client.get(param_key)
    if param_data:
        try:
            param_data = json.loads(param_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="Parameter data corrupted")
    else:
        param_data = None

    return {
        "license_status": license_status,
        "license_expiry": license_expiry,
        "params": param_data
    }


@licnese_router.get("/get-redis-data")
async def get_redis_data(request: Request):
    sync_key = request.headers.get("X-Sync-Key")
    if sync_key != SYNC_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    #Load Licenses
    licenses = []
    async for key in redis_client.scan_iter("license:*"):
        to_date = await redis_client.get(key)
        _, module, serial_num, company = key.split(":", 3)
        licenses.append({
            "module_name": module,
            "serial_num": serial_num,
            "company": company,
            "to_date": to_date
        })

    #Load Parameters
    params = []
    async for key in redis_client.scan_iter("params:*"):
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        _, module, serial_num, company = key.split(":", 3)
        params.append({
            "module_name": module,
            "serial_num": serial_num,
            "company": company,
            "params": data
        })

    return {"licenses": licenses, "params": params}


@licnese_router.get("/sync-redis-data")
async def sync_license(request: Request):
    sync_key = request.headers.get("X-Sync-Key")
    if sync_key != SYNC_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    result = await initialize_admin_data()

    return result


@licnese_router.post("/sync-single-license")
async def sync_single_license(body:Dict, request: Request):
    sync_key = request.headers.get("X-Sync-Key")
    if sync_key != SYNC_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    #print(f"Received body: {json.dumps(body)}")
    
    module_name = body.get("module_name")
    serial_num = body.get("serial_num")
    company_code = body.get("company_code")
    to_date = body.get("to_date")
    operation = body.get("operation", "upsert").lower()
    
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    key = f"license:{module_name}:{serial_num}:{company_code}"
    exists = await redis_client.exists(key)

    if operation == "delete":
        if exists:
            await redis_client.delete(key)
            return {"status": "deleted", "key": key}
        return {"status": "not_found", "key": key}

    # upsert path
    ttl = 86400 if to_date else 3600
    await redis_client.setex(key, ttl, to_date or "")
    
    response = {"status": "created" if not exists else "updated", "key": key}
    
    #print(f"Response: {json.dumps(response)}")
    
    return response


@licnese_router.post("/sync-single-param")
async def sync_single_param(body: Dict, request: Request):
    sync_key = request.headers.get("X-Sync-Key")
    if sync_key != SYNC_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    #print(f"Received body: {json.dumps(body)}")
    
    module_name = body.get("module_name")
    serial_num = body.get("serial_num")
    company_code = body.get("company_code")
    operation = body.get("operation", "upsert").lower()
    params = body.get("params")

    if not module_name or not serial_num or not company_code:
        raise HTTPException(status_code=400, detail="Missing module_name or serial_num or company_code")

    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    key = f"params:{module_name}:{serial_num}:{company_code}"
    exists = await redis_client.exists(key)

    if operation == "delete":
        if exists:
            await redis_client.delete(key)
            return {"status": "deleted", "key": key}
        return {"status": "not_found", "key": key}

    # upsert path
    await redis_client.set(key, json.dumps(params))
    
    response = {"status": "created" if not exists else "updated", "key": key}
    
    #print(f"Response: {json.dumps(response)}")
    
    return response




