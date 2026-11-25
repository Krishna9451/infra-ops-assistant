pipeline {
    agent any

    environment {
        IMAGE_NAME = 'infra-ops-assistant'
        IMAGE_TAG = "${BUILD_NUMBER}"
        PATH = "/usr/local/bin:/opt/homebrew/bin:${env.PATH}"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Install Dependencies') {
            steps {
                sh '''
                    python3 -m venv .ci-venv
                    .ci-venv/bin/pip install -r requirements-dev.txt
                    .ci-venv/bin/pip install -r requirements-agent.txt
                '''
            }
        }

        stage('Run Tests') {
            steps {
                sh '.ci-venv/bin/python -m pytest -q'
            }
        }

        stage('Build Docker Image') {
            steps {
                sh 'docker build --pull --no-cache -t $IMAGE_NAME:$IMAGE_TAG .'
            }
        }

        stage('Security Scan') {
            steps {
                sh '''
                    trivy image \
                    --exit-code 1 \
                    --severity HIGH,CRITICAL \
                    --ignore-unfixed \
                    $IMAGE_NAME:$IMAGE_TAG
                '''
            }
        }
        stage('Push to Docker Hub') {
            steps {
               withCredentials([usernamePassword(
               credentialsId: 'dockerhub-infra-credentials',
               usernameVariable: 'DOCKER_USER',
               passwordVariable: 'DOCKER_TOKEN'
               )]) {
            sh '''
                set +x

                echo "$DOCKER_TOKEN" | docker login \
                    -u "$DOCKER_USER" --password-stdin

                trap 'docker logout' EXIT

                docker tag $IMAGE_NAME:$IMAGE_TAG \
                    $DOCKER_USER/$IMAGE_NAME:$IMAGE_TAG

                docker push $DOCKER_USER/$IMAGE_NAME:$IMAGE_TAG
            '''
              }
            }
         }
         stage('Deploy Application') {
            steps {
              withCredentials([string(
              credentialsId: 'groq-api-key',
              variable: 'LLM_API_KEY'
               )]) {
            sh '''
                set +x
                set -eu

                docker rm -f infraops-container 2>/dev/null || true

                docker run -d \
                    --name infraops-container \
                    --restart unless-stopped \
                    -p 127.0.0.1:8001:8000 \
                    -v infraops-data:/data \
                    -e LLM_API_KEY \
                    -e LLM_BASE_URL=https://api.groq.com/openai/v1 \
                    -e LLM_MODEL=openai/gpt-oss-20b \
                    $IMAGE_NAME:$IMAGE_TAG
            '''
               }
            }
   }
    }



    post {
        success {
            echo 'Pipeline completed successfully!'
        }
        failure {
            echo 'Pipeline failed. Check console logs.'
        }
    }
}