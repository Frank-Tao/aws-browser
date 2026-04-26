# aws-browser

A small browser-based S3 file manager where AWS credentials stay on the backend.

The browser reads a local folder, sends a manifest to the backend, uploads each file through the backend, then finalizes the upload session. The backend recreates the folder structure in S3 with boto3 object keys.

## Features

- Browse an S3 bucket/prefix.
- Upload a local folder without browser-to-S3 access.
- Preserve local relative paths in S3 object keys.
- Skip `.obsidian` folders by default.
- Download S3 objects through the backend.
- Preview UTF-8 text objects up to 2 MB.
- Deploy frontend as static hosting, such as Vercel.
- Deploy backend to AWS API Gateway + Lambda with SAM.

## Project Layout

```text
backend/app/main.py            FastAPI routes
backend/app/s3_service.py      boto3 S3 wrapper
backend/app/session_store.py   in-memory local store or S3 manifest deployed store
backend/lambda_handler.py      Mangum adapter for Lambda
frontend/index.html            browser UI
frontend/app.js                folder upload and S3 browser logic
frontend/scripts/build.mjs     static frontend build
template.yaml                  AWS SAM API/Lambda stack
.github/workflows/             backend and frontend deployment workflows
```

## Local Setup

```bash
cd "$HOME/codes/aws-browser"
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements-dev.txt
cp backend/.env.example backend/.env
```

Edit `backend/.env`:

```env
S3_BUCKET=your-bucket-name
APP_AWS_REGION=ap-southeast-2
AWS_PROFILE=
S3_BASE_PREFIX=
MAX_FILE_BYTES=104857600
MAX_MANIFEST_FILES=5000
ALLOWED_ORIGINS=
UPLOAD_SESSION_STORE=auto
SESSION_MANIFEST_PREFIX=.aws-browser/sessions
APP_API_TOKEN=
```

For local S3 access, the backend needs AWS credentials. The simplest setup is an AWS CLI profile:

```bash
aws configure --profile your-profile
```

Then set:

```env
AWS_PROFILE=your-profile
```

Alternatively, export credentials before starting uvicorn:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_SESSION_TOKEN=... # only if using temporary credentials
```

Check credentials outside the app:

```bash
aws sts get-caller-identity --profile your-profile
aws s3 ls s3://your-bucket-name/ --profile your-profile
```

Run locally:

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

## API Pattern

```text
POST /api/upload-manifest
POST /api/upload-file
POST /api/finish-upload
GET  /api/list?prefix=
GET  /api/download?key=
GET  /api/read-text?key=
```

The upload flow is intentionally split into three calls so progress and retry behavior are straightforward.

## Run And Test

Backend local run:

```bash
cd "$HOME/codes/aws-browser"
source .venv/bin/activate
uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

If you created the virtualenv inside `backend/`, run it from the `backend` folder with the backend-local import path:

```bash
cd "$HOME/codes/aws-browser/backend"
source .venv/bin/activate
uvicorn local_main:app --reload --host 127.0.0.1 --port 8000
```

Backend tests:

```bash
cd "$HOME/codes/aws-browser"
source .venv/bin/activate
pytest backend/tests
```

Frontend local static build:

```bash
cd "$HOME/codes/aws-browser/frontend"
npm install
npm run build
```

Frontend smoke test:

```bash
cd "$HOME/codes/aws-browser/frontend"
npm test
```

Open the local integrated app:

```text
http://127.0.0.1:8000
```

## Frontend Deployment

The frontend is static. It builds to `frontend/dist`.

```bash
cd frontend
npm install
AWS_BROWSER_API_BASE_URL="https://your-api-id.execute-api.ap-southeast-2.amazonaws.com" npm run build
```

For Vercel, either use `frontend/vercel.json` with project root set to `frontend`, or use the repo-root `vercel.json`.

GitHub Actions workflow:

```text
.github/workflows/deploy-frontend-vercel.yml
```

Required secrets:

```text
VERCEL_TOKEN
VERCEL_ORG_ID
VERCEL_PROJECT_ID
```

Configure these as Vercel project environment variables because `vercel build` runs the static build:

```text
AWS_BROWSER_API_BASE_URL
AWS_BROWSER_API_TOKEN
```

Do not treat `AWS_BROWSER_API_TOKEN` as strong secrecy if it is embedded in a public static frontend. For a personal/private deployment it is a simple gate, not a full auth system.

## Backend Deployment

The backend deploys with AWS SAM:

```bash
sam build
sam deploy \
  --stack-name aws-browser-api \
  --region ap-southeast-2 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    S3Bucket=your-bucket-name \
    AwsRegionName=ap-southeast-2 \
    AllowedOrigins=https://your-frontend-domain.vercel.app \
    AppApiToken=optional-token \
    SessionManifestPrefix=.aws-browser/sessions \
    MaxLambdaConcurrency=5 \
    ApiThrottleBurstLimit=5 \
    ApiThrottleRateLimit=5
```

The SAM stack creates:

- API Gateway HTTP API
- Lambda function
- S3 manifest objects for upload session state
- IAM permissions for `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject`
- Lambda max-concurrency cap to control scale-out
- API Gateway request throttling

GitHub Actions workflow:

```text
.github/workflows/deploy-backend.yml
```

Required GitHub secrets:

```text
AWS_ROLE_TO_ASSUME
AWS_BROWSER_S3_BUCKET
```

Optional GitHub secrets/vars:

```text
secrets.AWS_BROWSER_API_TOKEN
vars.AWS_REGION
vars.BACKEND_STACK_NAME
vars.S3_BASE_PREFIX
vars.FRONTEND_ORIGIN
vars.MAX_FILE_BYTES
vars.MAX_MANIFEST_FILES
vars.SESSION_MANIFEST_PREFIX
vars.MAX_LAMBDA_CONCURRENCY
vars.API_THROTTLE_BURST_LIMIT
vars.API_THROTTLE_RATE_LIMIT
```

By default, deployed upload state is written to:

```text
<S3_BASE_PREFIX>/.aws-browser/sessions/<session-id>/
```

Those internal control objects are hidden from the browser listing and removed when `/api/finish-upload` completes. If a session is abandoned, the objects are small JSON files; add an S3 lifecycle rule for the `.aws-browser/sessions/` prefix if you want automatic cleanup.

The Lambda max-concurrency cap defaults to:

```text
MaxLambdaConcurrency=5
```

Set it to any value from `1` to `10`. Use `5` for controlled batch work, or `10` if you want more parallelism. In AWS Lambda, the direct function-level maximum concurrency control is configured through the `ReservedConcurrentExecutions` property, so the template uses that property behind the friendlier `MaxLambdaConcurrency` parameter. Extra concurrent requests will be throttled instead of scaling without limit.

API Gateway also throttles requests before they reach Lambda:

```text
ApiThrottleBurstLimit=5
ApiThrottleRateLimit=5
```

Raise these together with `MaxLambdaConcurrency` if you want a larger batch window.

## SAM CLI

Install the SAM CLI if you want to run `sam validate`, `sam build`, or deploy from your laptop.

AWS’s official macOS recommendation is the first-party package installer. For Apple silicon, download the `aws-sam-cli-macos-arm64.pkg`; for Intel Macs, download the `aws-sam-cli-macos-x86_64.pkg`. After installation, verify:

```bash
which sam
sam --version
```

AWS notes that its old managed Homebrew tap is no longer maintained, though a community-managed Homebrew installer exists. Official install docs: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

After installing:

```bash
cd "$HOME/codes/aws-browser"
sam validate --lint
sam build
```

## Important Limit

API Gateway + Lambda is not suitable for large file proxy uploads. API Gateway request payloads are limited, so the deployed SAM default sets:

```text
MaxFileBytes=6000000
```

That is fine for Markdown notes and small documents. For large files, use ECS/App Runner, or add a chunked upload protocol where each browser chunk is proxied through the backend and assembled with S3 multipart upload.
