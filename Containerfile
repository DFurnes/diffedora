FROM registry.fedoraproject.org/fedora:44
RUN dnf install -y ostree rpm-ostree python3 && dnf clean all
COPY diffedora.py /usr/local/bin/diffedora
ENTRYPOINT ["python3", "/usr/local/bin/diffedora"]
