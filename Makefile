.PHONY: bootstrap run test migrate makemigrations shell

bootstrap:
	python scripts/bootstrap.py

run:
	@echo "Open: https://$${CODESPACE_NAME}-8000.$${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
	. .venv/bin/activate && python manage.py runserver 0.0.0.0:8000

migrate:
	. .venv/bin/activate && python manage.py migrate

makemigrations:
	. .venv/bin/activate && python manage.py makemigrations

shell:
	. .venv/bin/activate && python manage.py shell

test:
	. .venv/bin/activate && python manage.py test
