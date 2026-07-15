.PHONY: check clean

check:
	sh -n install.sh uninstall.sh scripts/doctor.sh daemon/px13-powerd.sleep
	python3 -m py_compile daemon/px13_powerd.py daemon/px13ctl.py daemon/px13_benchmark.py
	$(MAKE) -C nvpcf-override clean all
	$(MAKE) -C module clean all

clean:
	$(MAKE) -C nvpcf-override clean
	$(MAKE) -C module clean
	rm -rf daemon/__pycache__
