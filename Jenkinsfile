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
                sh 'docker build -t $IMAGE_NAME:$IMAGE_TAG .'
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