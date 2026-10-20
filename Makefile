.PHONY: check clean

check:
	sh -n install.sh uninstall.sh scripts/doctor.sh daemon/px13-powerd.sleep
	python3 -m py_compile daemon/px13_powerd.py daemon/px13ctl.py daemon/px13_benchmark.py daemon/px13power/*.py
	python3 -m unittest discover -s tests
	$(MAKE) -C nvpcf-override clean all
	$(MAKE) -C module clean all

clean:
	$(MAKE) -C nvpcf-override clean
	$(MAKE) -C module clean
	rm -rf daemon/__pycache__ daemon/px13power/__pycache__ tests/__pycache__
