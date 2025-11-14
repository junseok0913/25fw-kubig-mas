FROM public.ecr.aws/lambda/python:3.11

WORKDIR /var/task

# 애플리케이션 코드 복사
COPY Lambda ./Lambda

# 파이썬 의존성 설치 (BeautifulSoup + requests + boto3)
RUN pip install -r Lambda/requirements.txt

# Lambda 핸들러 설정 (모듈.함수)
CMD ["Lambda.aws_lambda_handler.handler"]
