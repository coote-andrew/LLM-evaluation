.PHONY: bootstrap run test migrate makemigrations shell

PYTHON = python3

bootstrap:
	$(PYTHON) scripts/bootstrap.py

run:
	@echo "Open: https://$${CODESPACE_NAME}-8000.$${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
	$(PYTHON) manage.py runserver 0.0.0.0:8000

migrate:
	$(PYTHON) manage.py migrate

makemigrations:
	$(PYTHON) manage.py makemigrations

shell:
	$(PYTHON) manage.py shell

test:
	$(PYTHON) manage.py test --settings=config.test_settings
