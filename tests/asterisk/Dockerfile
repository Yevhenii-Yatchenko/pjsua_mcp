FROM andrius/asterisk:latest

USER root
COPY pjsip.conf /etc/asterisk/pjsip.conf
COPY extensions.conf /etc/asterisk/extensions.conf
COPY modules.conf /etc/asterisk/modules.conf
RUN chown asterisk:asterisk /etc/asterisk/pjsip.conf \
    /etc/asterisk/extensions.conf /etc/asterisk/modules.conf
USER asterisk
