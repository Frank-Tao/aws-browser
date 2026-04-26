# aws-browser

Local-first S3 file browser and uploader:
- Frontend: static UI (`frontend/`)
- Backend: FastAPI (`backend/`)
- Cloud deploy: Vercel (frontend) + AWS SAM (API Gateway + Lambda)

## Quick Start (Local)

1. Install deps
```bash
cd /Users/franktao/codes/aws-browser/frontend
npm install
```

2. Configure backend env
```bash
cp /Users/franktao/codes/aws-browser/backend/.env.example /Users/franktao/codes/aws-browser/backend/.env
```

Edit `backend/.env` at minimum:
```env
S3_BUCKET=adaptive-player
APP_AWS_REGION=ap-southeast-2
AWS_PROFILE=aws-browser
```

3. Start local app (backend serves frontend)
```bash
cd /Users/franktao/codes/aws-browser/frontend
source ../.venv/bin/activate
npm run local
```

Open:
`http://127.0.0.1:8000`

If your virtualenv is in `backend/.venv`, use:
```bash
cd /Users/franktao/codes/aws-browser/frontend
source ../backend/.venv/bin/activate
npm run local:backenddir
```

## NPM Commands

Run from `frontend/`:

```bash
npm run build
npm run test
npm run local
npm run local:backenddir
npm run deploy:vercel
```

## Upload Modes

Upload tab now has:
- `Use JSON payload upload mode` (default unchecked)

Behavior:
- Unchecked: old multipart endpoint `POST /api/upload-file`
- Checked: frontend reads file content in JavaScript, base64-encodes it, and sends JSON to `POST /api/upload-file-json`

JSON mode is useful if multipart upload is blocked by network/proxy policy.

## Frontend Deploy (Vercel)

GitHub Actions now resolves backend `ApiUrl` from CloudFormation automatically and injects it into frontend build (`AWS_BROWSER_API_BASE_URL`).

Required GitHub secrets for frontend workflow:
- `VERCEL_TOKEN`
- `VERCEL_ORG_ID`
- `VERCEL_PROJECT_ID`
- `AWS_ROLE_TO_ASSUME` (used to read CloudFormation output)

Optional:
- `AWS_BROWSER_API_TOKEN` (if backend token auth is enabled)

Deploy:
```bash
cd /Users/franktao/codes/aws-browser/frontend
npm run deploy:vercel
```

## Backend Deploy (AWS SAM)

```bash
cd /Users/franktao/codes/aws-browser
sam validate --lint
sam build --use-container
sam deploy \
  --stack-name aws-browser-api \
  --region ap-southeast-2 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    S3Bucket=adaptive-player \
    AwsRegionName=ap-southeast-2 \
    AllowedOrigins='https://<your-vercel-domain>' \
    SessionManifestPrefix=.aws-browser/sessions \
    MaxLambdaConcurrency=5 \
    ApiThrottleBurstLimit=5 \
    ApiThrottleRateLimit=5
```

Get API URL:
```bash
aws cloudformation describe-stacks \
  --stack-name aws-browser-api \
  --region ap-southeast-2 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text
```

Backend GitHub Actions workflow now auto-runs a frontend deploy job after backend deploy, using the latest API URL.

## Notes

- If `APP_API_TOKEN` is empty in backend deploy, auth header is not required.
- Vercel static frontend does not host `/api/*`; API requests must go to API Gateway URL.
- Local `.env` does not configure Vercel production. Use Vercel environment variables.
